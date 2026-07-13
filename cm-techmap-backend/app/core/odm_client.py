"""
CM TECHMAP — NodeODM REST API Client
Async client for photogrammetry processing via OpenDroneMap.
"""

import json
import logging
from pathlib import Path
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


class NodeODMError(Exception):
    """Custom exception for NodeODM API errors."""
    pass


class NodeODMClient:
    """
    Async HTTP client for the NodeODM REST API.

    NodeODM API reference:
    - POST /task/new — Create a new processing task
    - GET  /task/{uuid}/info — Get task status and progress
    - GET  /task/{uuid}/download/{asset} — Download a result asset
    - POST /task/{uuid}/cancel — Cancel a running task
    - POST /task/{uuid}/remove — Remove a completed/failed task
    - GET  /task/list — List all tasks
    - GET  /info — Node capabilities and version
    """

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        timeout: int | None = None,
    ):
        self.host = host or settings.nodeodm_host
        self.port = port or settings.nodeodm_port
        self.timeout = timeout or settings.nodeodm_timeout
        self.base_url = f"http://{self.host}:{self.port}"

    async def get_node_info(self) -> dict[str, Any]:
        """Get NodeODM node capabilities, version, and available options."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(f"{self.base_url}/info")
            resp.raise_for_status()
            return resp.json()

    async def is_healthy(self) -> bool:
        """Check if the NodeODM node is reachable and ready."""
        try:
            info = await self.get_node_info()
            return info.get("version") is not None
        except Exception:
            return False

    async def create_task(
        self,
        image_paths: list[str],
        options: dict[str, Any] | None = None,
        name: str = "",
    ) -> str:
        """
        Create a new photogrammetry task on NodeODM.

        Args:
            image_paths: List of local file paths to upload
            options: ODM processing options (dsm, dtm, orthophoto-resolution, etc.)
            name: Human-readable task name

        Returns:
            Task UUID string
        """
        if options is None:
            options = json.loads(settings.nodeodm_default_options)

        # Build options as NodeODM expects: [{"name": "key", "value": val}, ...]
        options_list = [{"name": k, "value": v} for k, v in options.items()]

        files = []
        for path in image_paths:
            p = Path(path)
            if p.exists():
                files.append(("images", (p.name, open(p, "rb"), "image/jpeg")))

        if not files:
            raise NodeODMError(f"No valid image files found in {image_paths}")

        data = {
            "name": name or "cm-techmap-processing",
            "options": json.dumps(options_list),
        }

        logger.info(
            f"[ODM] Creating task with {len(files)} images, options: {options}"
        )

        try:
            async with httpx.AsyncClient(timeout=300) as client:
                resp = await client.post(
                    f"{self.base_url}/task/new",
                    data=data,
                    files=files,
                )
                resp.raise_for_status()
                result = resp.json()

                task_uuid = result.get("uuid")
                if not task_uuid:
                    raise NodeODMError(f"No UUID in response: {result}")

                logger.info(f"[ODM] Task created: {task_uuid}")
                return task_uuid
        finally:
            # Close all file handles
            for _, file_tuple in files:
                file_tuple[1].close()

    async def create_task_from_urls(
        self,
        image_urls: list[str],
        options: dict[str, Any] | None = None,
        name: str = "",
    ) -> str:
        """
        Create a task using image URLs (for MinIO presigned URLs).
        NodeODM will download the images itself.
        """
        if options is None:
            options = json.loads(settings.nodeodm_default_options)

        options_list = [{"name": k, "value": v} for k, v in options.items()]

        payload = {
            "name": name or "cm-techmap-processing",
            "options": json.dumps(options_list),
        }

        # Upload empty task with options, then add images via URL
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/task/new/init",
                data=payload,
            )
            resp.raise_for_status()
            task_uuid = resp.json().get("uuid")

            # Upload images by URL
            for url in image_urls:
                await client.post(
                    f"{self.base_url}/task/new/upload/{task_uuid}",
                    json={"images": [{"url": url}]},
                )

            # Commit the task to start processing
            await client.post(
                f"{self.base_url}/task/new/commit/{task_uuid}",
            )

        logger.info(f"[ODM] Task created from URLs: {task_uuid}")
        return task_uuid

    async def get_task_info(self, task_uuid: str) -> dict[str, Any]:
        """
        Get detailed task information including status and progress.

        Returns dict with keys:
            - uuid, name, status (codes: 10=QUEUED, 20=RUNNING, 30=FAILED,
              40=COMPLETED, 50=CANCELED)
            - processingTime, progress (0-100)
            - output (console log lines)
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(
                f"{self.base_url}/task/{task_uuid}/info"
            )
            resp.raise_for_status()
            return resp.json()

    async def get_task_progress(self, task_uuid: str) -> dict[str, Any]:
        """Get simplified progress info for WebSocket updates."""
        info = await self.get_task_info(task_uuid)
        status_code = info.get("status", {}).get("code", 0)

        status_map = {
            10: "queued",
            20: "running",
            30: "failed",
            40: "completed",
            50: "canceled",
        }

        return {
            "uuid": task_uuid,
            "status": status_map.get(status_code, "unknown"),
            "progress": info.get("progress", 0),
            "processing_time": info.get("processingTime", 0),
            "images_count": info.get("imagesCount", 0),
            "last_output": (info.get("output", []) or [])[-3:],
        }

    async def download_asset(
        self,
        task_uuid: str,
        asset: str,
        destination: str | Path,
    ) -> Path:
        """
        Download a processing result asset from NodeODM.

        Args:
            task_uuid: Task UUID
            asset: Asset name — one of:
                - 'orthophoto.tif' (Orthomosaic)
                - 'dsm.tif' (Digital Surface Model)
                - 'dtm.tif' (Digital Terrain Model)
                - 'georeferenced_model.laz' (Point Cloud)
                - 'textured_model.zip' (3D Mesh)
                - 'all.zip' (Everything)
            destination: Local path to save the file

        Returns:
            Path to the downloaded file
        """
        dest = Path(destination)
        dest.parent.mkdir(parents=True, exist_ok=True)

        logger.info(f"[ODM] Downloading {asset} from task {task_uuid}")

        async with httpx.AsyncClient(timeout=600) as client:
            async with client.stream(
                "GET",
                f"{self.base_url}/task/{task_uuid}/download/{asset}",
            ) as resp:
                resp.raise_for_status()
                with open(dest, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        f.write(chunk)

        file_size = dest.stat().st_size
        logger.info(
            f"[ODM] Downloaded {asset}: {dest} ({file_size / 1024 / 1024:.1f} MB)"
        )
        return dest

    async def get_available_assets(self, task_uuid: str) -> list[str]:
        """
        Discover which result assets are available for download from a completed task.

        NodeODM produces different assets depending on processing options:
        - orthophoto.tif — Always produced
        - dsm.tif — When --dsm flag is set
        - dtm.tif — When --dtm flag is set
        - georeferenced_model.laz — Point cloud (always)
        - textured_model.zip — 3D mesh + textures (when --skip-3dmodel is false)
        - all.zip — Full archive of all outputs

        Returns:
            List of available asset names that can be passed to download_asset()
        """
        # Known ODM output assets to probe
        known_assets = [
            "orthophoto.tif",
            "dsm.tif",
            "dtm.tif",
            "georeferenced_model.laz",
            "textured_model.zip",
        ]

        available = []
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for asset_name in known_assets:
                try:
                    # HEAD request to check if asset exists without downloading
                    resp = await client.head(
                        f"{self.base_url}/task/{task_uuid}/download/{asset_name}",
                    )
                    if resp.status_code == 200:
                        available.append(asset_name)
                except Exception:
                    continue

        logger.info(f"[ODM] Available assets for {task_uuid}: {available}")
        return available

    async def download_all_assets(
        self,
        task_uuid: str,
        dest_dir: str | Path,
        include_3d_model: bool = True,
    ) -> dict[str, Path]:
        """
        Download all available processing results from a completed NodeODM task.

        This is the production-grade download method that:
        1. Probes for available assets first (avoids 404 errors)
        2. Downloads each asset with appropriate timeouts
        3. Returns a mapping of asset_name → local_path

        Args:
            task_uuid: Completed task UUID
            dest_dir: Local directory to save files
            include_3d_model: Whether to download textured_model.zip (large file)

        Returns:
            Dict mapping asset names to local file paths
        """
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)

        available = await self.get_available_assets(task_uuid)

        # Asset name → local filename mapping
        asset_map = {
            "orthophoto.tif": "orthophoto.tif",
            "dsm.tif": "dsm.tif",
            "dtm.tif": "dtm.tif",
            "georeferenced_model.laz": "pointcloud.laz",
            "textured_model.zip": "textured_model.zip",
        }

        downloaded: dict[str, Path] = {}

        for asset_name in available:
            if asset_name == "textured_model.zip" and not include_3d_model:
                logger.info(f"[ODM] Skipping {asset_name} (include_3d_model=False)")
                continue

            local_name = asset_map.get(asset_name, asset_name)
            local_path = dest_dir / local_name

            try:
                # Use extended timeout for large assets (3D models can be >1GB)
                timeout = 1800 if asset_name == "textured_model.zip" else 600
                async with httpx.AsyncClient(timeout=timeout) as client:
                    async with client.stream(
                        "GET",
                        f"{self.base_url}/task/{task_uuid}/download/{asset_name}",
                    ) as resp:
                        resp.raise_for_status()
                        with open(local_path, "wb") as f:
                            async for chunk in resp.aiter_bytes(chunk_size=131072):
                                f.write(chunk)

                file_size = local_path.stat().st_size
                downloaded[asset_name] = local_path
                logger.info(
                    f"[ODM] Downloaded {asset_name}: "
                    f"{file_size / 1024 / 1024:.1f} MB"
                )
            except Exception as e:
                logger.warning(f"[ODM] Failed to download {asset_name}: {e}")

        logger.info(
            f"[ODM] Bulk download complete: {len(downloaded)}/{len(available)} assets"
        )
        return downloaded

    async def get_task_output_log(
        self, task_uuid: str, tail: int = 100
    ) -> list[str]:
        """
        Retrieve the processing output log from a NodeODM task.

        Useful for diagnostics when processing fails or for capturing
        quality metrics (reprojection error, point count, etc.).

        Args:
            task_uuid: Task UUID
            tail: Number of log lines to return (from the end)

        Returns:
            List of log line strings
        """
        try:
            info = await self.get_task_info(task_uuid)
            output = info.get("output", []) or []
            return output[-tail:] if len(output) > tail else output
        except Exception as e:
            logger.warning(f"[ODM] Could not retrieve output log: {e}")
            return []

    async def cancel_task(self, task_uuid: str) -> bool:
        """Cancel a running task."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/task/cancel",
                json={"uuid": task_uuid},
            )
            return resp.status_code == 200

    async def remove_task(self, task_uuid: str) -> bool:
        """Remove a completed/failed task and its data from NodeODM."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/task/remove",
                json={"uuid": task_uuid},
            )
            return resp.status_code == 200

    async def list_tasks(self) -> list[dict[str, Any]]:
        """List all tasks on the NodeODM node."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(f"{self.base_url}/task/list")
            resp.raise_for_status()
            return resp.json()
