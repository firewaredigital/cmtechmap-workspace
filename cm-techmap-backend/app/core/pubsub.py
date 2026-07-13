"""
CM TECHMAP — Redis Pub/Sub for Real-Time Progress
Bridges Celery workers and FastAPI WebSocket endpoints.
"""

import asyncio
import json
import logging
from typing import Any, AsyncIterator

import redis.asyncio as aioredis

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

CHANNEL_PREFIX = "cm_techmap:progress:"

# Timeout for waiting on Pub/Sub messages (seconds).
# If no message arrives within this window, yield a heartbeat-style None
# so the WebSocket handler knows the subscription is still alive.
PUBSUB_POLL_TIMEOUT = 30.0


def _get_sync_redis():
    """Get a synchronous Redis client (for use in Celery workers)."""
    import redis
    return redis.from_url(settings.redis_url, decode_responses=True)


async def _get_async_redis() -> aioredis.Redis:
    """Get an async Redis client (for use in FastAPI)."""
    return aioredis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=10,
        retry_on_timeout=True,
    )


def publish_progress(
    task_id: str,
    stage: str,
    progress: int,
    message: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    """
    Publish a progress update from a Celery worker (synchronous).
    Called from within Celery tasks.
    """
    channel = f"{CHANNEL_PREFIX}{task_id}"
    payload = {
        "task_id": task_id,
        "stage": stage,
        "progress": min(max(progress, 0), 100),
        "message": message,
    }
    if extra:
        payload.update(extra)

    r = _get_sync_redis()
    try:
        r.publish(channel, json.dumps(payload))
        # Also store latest state in a key for late joiners
        r.setex(
            f"cm_techmap:state:{task_id}",
            3600,  # TTL: 1 hour
            json.dumps(payload),
        )
    except Exception as e:
        logger.error(f"[PubSub] Failed to publish progress for {task_id}: {e}")
    finally:
        r.close()


async def subscribe_progress(task_id: str) -> AsyncIterator[dict[str, Any]]:
    """
    Subscribe to progress updates for a task (async generator).
    Used by WebSocket handlers to stream updates to clients.

    This generator:
    - First yields the last known state (for late joiners)
    - Then subscribes to the Redis Pub/Sub channel for live updates
    - Automatically terminates when the task reaches a final state
    - Uses a polling timeout to detect stale connections
    """
    channel = f"{CHANNEL_PREFIX}{task_id}"
    r = None
    pubsub = None

    try:
        r = await _get_async_redis()

        # First, send the last known state (for late joiners)
        last_state = await r.get(f"cm_techmap:state:{task_id}")
        if last_state:
            data = json.loads(last_state)
            yield data
            # If task already completed, stop immediately
            if data.get("stage") in ("completed", "failed", "canceled"):
                return

        # Subscribe to live updates
        pubsub = r.pubsub()
        await pubsub.subscribe(channel)

        # Use get_message with timeout instead of the blocking listen()
        # This allows the generator to be cancelled cleanly
        max_idle_iterations = 0
        while True:
            try:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=PUBSUB_POLL_TIMEOUT,
                )

                if message and message["type"] == "message":
                    max_idle_iterations = 0  # Reset idle counter
                    data = json.loads(message["data"])
                    yield data

                    # Stop if the task is done
                    if data.get("stage") in ("completed", "failed", "canceled"):
                        break
                else:
                    # No message received within timeout — connection still alive
                    max_idle_iterations += 1
                    # After 20 idle timeouts (~10 minutes), assume task is dead
                    if max_idle_iterations > 20:
                        logger.warning(
                            f"[PubSub] No updates for task {task_id} "
                            f"after {max_idle_iterations * PUBSUB_POLL_TIMEOUT}s, "
                            f"closing subscription"
                        )
                        break
                    # Yield nothing (the caller keeps waiting)
                    await asyncio.sleep(0.1)

            except asyncio.CancelledError:
                logger.info(f"[PubSub] Subscription cancelled for task {task_id}")
                break
            except Exception as e:
                logger.error(f"[PubSub] Error reading message for {task_id}: {e}")
                # Brief backoff before retrying
                await asyncio.sleep(1.0)

    except aioredis.ConnectionError as e:
        logger.error(f"[PubSub] Redis connection failed for task {task_id}: {e}")
        # Yield a failure message so the WebSocket can inform the client
        yield {
            "task_id": task_id,
            "stage": "error",
            "progress": 0,
            "message": f"Real-time connection failed: {e}",
            "type": "error",
        }
    except Exception as e:
        logger.error(f"[PubSub] Unexpected error for task {task_id}: {e}")
    finally:
        if pubsub:
            try:
                await pubsub.unsubscribe(channel)
            except Exception:
                pass
        if r:
            try:
                await r.aclose()
            except Exception:
                pass


async def get_last_progress(task_id: str) -> dict[str, Any] | None:
    """Get the last known progress state for a task."""
    r = await _get_async_redis()
    try:
        data = await r.get(f"cm_techmap:state:{task_id}")
        return json.loads(data) if data else None
    except aioredis.ConnectionError as e:
        logger.warning(f"[PubSub] Redis unavailable for get_last_progress: {e}")
        return None
    except Exception as e:
        logger.warning(f"[PubSub] Error fetching last progress for {task_id}: {e}")
        return None
    finally:
        try:
            await r.aclose()
        except Exception:
            pass
