"""CM TECHMAP — WebSocket Endpoint for Real-Time Processing Progress"""

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.pubsub import subscribe_progress, get_last_progress

router = APIRouter(tags=["WebSocket"])
logger = logging.getLogger(__name__)

# Heartbeat interval in seconds — keeps the connection alive through proxies
HEARTBEAT_INTERVAL = 15
# Maximum idle time (no updates) before closing the connection
MAX_IDLE_SECONDS = 600  # 10 minutes


@router.websocket("/ws/processing/{task_id}")
async def ws_processing_progress(websocket: WebSocket, task_id: str):
    """
    WebSocket endpoint for real-time processing progress updates.

    Protocol:
    1. Client connects to ws://host/ws/processing/{celery_task_id}
    2. Server sends the last known state immediately (for late joiners)
    3. Server streams progress updates as JSON: {"stage": "...", "progress": 42, ...}
    4. Server closes connection when task completes/fails/cancels
    5. Server sends heartbeat pings every 15s to keep connection alive

    Message format:
    {
        "task_id": "abc123",
        "stage": "odm_processing",
        "progress": 65,
        "message": "Generating orthophoto...",
        "odm_status": "running",
        "processing_time": 342
    }
    """
    await websocket.accept()
    logger.info(f"[WS] Client connected for task {task_id}")

    try:
        # Send initial connection acknowledgement
        await websocket.send_json({
            "type": "connected",
            "task_id": task_id,
            "message": "Connected to processing progress stream",
        })

        # Send last known state for late joiners
        last_state = await get_last_progress(task_id)
        if last_state:
            await websocket.send_json(last_state)
            # If task already finished, close immediately
            if last_state.get("stage") in ("completed", "failed", "canceled"):
                logger.info(
                    f"[WS] Task {task_id} already finished: {last_state.get('stage')}"
                )
                return

        # Create concurrent tasks: progress streaming + heartbeat + client listener
        progress_task = asyncio.create_task(
            _stream_progress(websocket, task_id)
        )
        heartbeat_task = asyncio.create_task(
            _heartbeat_loop(websocket, task_id)
        )
        # Listen for client messages (ping/pong, close)
        client_task = asyncio.create_task(
            _client_listener(websocket, task_id)
        )

        # Wait for any task to complete (first one wins)
        done, pending = await asyncio.wait(
            [progress_task, heartbeat_task, client_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Cancel remaining tasks
        for task in pending:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        # Check if progress task raised an exception
        for task in done:
            if task.exception() and not isinstance(
                task.exception(), (WebSocketDisconnect, asyncio.CancelledError)
            ):
                logger.error(
                    f"[WS] Task error for {task_id}: {task.exception()}"
                )

    except WebSocketDisconnect:
        logger.info(f"[WS] Client disconnected for task {task_id}")
    except Exception as e:
        logger.error(f"[WS] Error for task {task_id}: {e}")
        try:
            await websocket.send_json({
                "type": "error",
                "message": str(e),
            })
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


async def _stream_progress(websocket: WebSocket, task_id: str) -> None:
    """Stream progress updates from Redis Pub/Sub to the WebSocket client."""
    try:
        async for update in subscribe_progress(task_id):
            try:
                await websocket.send_json(update)

                # Check if processing is done
                if update.get("stage") in ("completed", "failed", "canceled"):
                    logger.info(
                        f"[WS] Task {task_id} finished: {update.get('stage')}"
                    )
                    return
            except WebSocketDisconnect:
                logger.info(f"[WS] Client disconnected during streaming for {task_id}")
                return
    except Exception as e:
        if not isinstance(e, (WebSocketDisconnect, asyncio.CancelledError)):
            logger.error(f"[WS] Progress stream error for {task_id}: {e}")
        raise


async def _heartbeat_loop(websocket: WebSocket, task_id: str) -> None:
    """Send periodic heartbeat pings to keep the connection alive."""
    try:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            try:
                await websocket.send_json({
                    "type": "heartbeat",
                    "task_id": task_id,
                })
            except WebSocketDisconnect:
                return
            except Exception:
                return
    except asyncio.CancelledError:
        pass


async def _client_listener(websocket: WebSocket, task_id: str) -> None:
    """Listen for client messages (handles pong responses and close frames)."""
    try:
        while True:
            try:
                message = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=MAX_IDLE_SECONDS,
                )
                # Handle client pong or ping messages
                if message == "ping":
                    await websocket.send_text("pong")
                elif message == "close":
                    logger.info(f"[WS] Client requested close for {task_id}")
                    return
            except asyncio.TimeoutError:
                logger.info(f"[WS] Connection idle timeout for {task_id}")
                return
    except WebSocketDisconnect:
        return
    except asyncio.CancelledError:
        pass


@router.websocket("/ws/heartbeat")
async def ws_heartbeat(websocket: WebSocket):
    """Simple heartbeat WebSocket for connection testing."""
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_text()
            await websocket.send_text(f"pong:{data}")
    except WebSocketDisconnect:
        pass
