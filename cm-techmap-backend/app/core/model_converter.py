"""
CM TECHMAP — 3D Model Converter (OBJ → glTF/glb)

Converts textured 3D models from NodeODM's photogrammetric pipeline
into web-optimized glTF/glb format for real-time rendering.

Architecture context from research transcript:
  - NodeODM outputs: textured_model.zip → OBJ + MTL + texture JPGs
  - OBJ files are NOT georeferenced (Cap. 5, Int. 4)
  - Vertex coordinates are in a local CRS (UTM or project-local)
  - The offset.xyz provides the translation vector T back to global coords
  - Frontend must apply RTE (Relative-To-Eye) with Float64 offset

This module handles:
  1. ZIP extraction and validation of model assets
  2. Vertex barycenter computation for offset.xyz generation
  3. OBJ → glTF/glb conversion via trimesh (when available) or obj2gltf
  4. Model metadata extraction (vertex count, triangle count, bbox)
"""

import json
import logging
import os
import shutil
import subprocess
import zipfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Data Structures
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class ModelFiles:
    """Paths to extracted model components."""
    obj_path: str = ""
    mtl_path: str = ""
    texture_paths: list[str] = None  # type: ignore[assignment]
    root_dir: str = ""

    def __post_init__(self):
        if self.texture_paths is None:
            self.texture_paths = []

    def is_valid(self) -> bool:
        return bool(self.obj_path) and os.path.exists(self.obj_path)


@dataclass
class ModelOffset:
    """Barycentric offset computed from OBJ vertex data."""
    x: float = 0.0       # Centroid X (Float64)
    y: float = 0.0       # Centroid Y (Float64)
    z: float = 0.0       # Centroid Z (Float64)
    vertex_count: int = 0
    bbox_min: list[float] = None  # type: ignore[assignment]
    bbox_max: list[float] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.bbox_min is None:
            self.bbox_min = [0.0, 0.0, 0.0]
        if self.bbox_max is None:
            self.bbox_max = [0.0, 0.0, 0.0]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ModelMetadata:
    """Metadata about a converted 3D model."""
    vertex_count: int = 0
    triangle_count: int = 0
    material_count: int = 0
    texture_count: int = 0
    file_size_bytes: int = 0
    format: str = "glb"
    bbox_local: dict[str, list[float]] = None  # type: ignore[assignment]
    offset: ModelOffset = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.bbox_local is None:
            self.bbox_local = {"min": [0, 0, 0], "max": [0, 0, 0]}

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        if self.offset:
            result["offset"] = self.offset.to_dict()
        return result


# ══════════════════════════════════════════════════════════════════════════════
# Core Functions
# ══════════════════════════════════════════════════════════════════════════════

def extract_textured_model(
    zip_path: str | Path,
    dest_dir: str | Path,
) -> ModelFiles:
    """
    Extract a textured 3D model from NodeODM's textured_model.zip output.

    NodeODM's textured_model.zip typically contains:
      - textured_model.obj — Wavefront OBJ mesh
      - textured_model.obj.mtl — Material library
      - textured_model_material0000_map_Kd.jpg — Texture atlas(es)

    Some versions nest files in an 'odm_texturing/' subdirectory.

    Args:
        zip_path: Path to the textured_model.zip from NodeODM
        dest_dir: Directory to extract files into

    Returns:
        ModelFiles with paths to extracted components
    """
    zip_path = Path(zip_path)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    if not zipfile.is_zipfile(zip_path):
        raise ValueError(f"Not a valid ZIP file: {zip_path}")

    logger.info(f"[MODEL] Extracting {zip_path.name} → {dest_dir}")

    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(dest_dir)

    # Locate model files (may be at root or in a subdirectory)
    obj_path = ""
    mtl_path = ""
    textures: list[str] = []

    for root, _, files in os.walk(dest_dir):
        for fname in files:
            full = os.path.join(root, fname)
            lower = fname.lower()
            if lower.endswith(".obj"):
                obj_path = full
            elif lower.endswith(".mtl"):
                mtl_path = full
            elif lower.endswith((".jpg", ".jpeg", ".png", ".tif", ".tiff")):
                textures.append(full)

    if not obj_path:
        # Some ODM versions use different file structures
        logger.warning("[MODEL] No .obj file found in ZIP. Listing contents:")
        for root, _, files in os.walk(dest_dir):
            for fname in files:
                logger.warning(f"  → {os.path.join(root, fname)}")
        raise FileNotFoundError(f"No OBJ file found in {zip_path.name}")

    model_files = ModelFiles(
        obj_path=obj_path,
        mtl_path=mtl_path,
        texture_paths=textures,
        root_dir=str(dest_dir),
    )

    logger.info(
        f"[MODEL] Extracted: OBJ={os.path.basename(obj_path)}, "
        f"MTL={'yes' if mtl_path else 'no'}, "
        f"Textures={len(textures)}"
    )
    return model_files


def compute_model_offset(obj_path: str | Path) -> ModelOffset:
    """
    Compute the barycentric offset (centroid) from OBJ vertex data.

    This implements the concept from the research transcript (Cap. 5):
    "OBJ files produced by photogrammetry software are shifted to be
    as close to x=0, y=0, z=0 as possible."

    For models that ARE already centered (small coordinate values),
    the offset will be near (0, 0, 0). For models with large UTM
    coordinates, this extracts the centroid for RTE rendering.

    The function reads only vertex lines ('v x y z') for efficiency,
    without loading the full mesh into memory.

    Args:
        obj_path: Path to the Wavefront OBJ file

    Returns:
        ModelOffset with centroid and bounding box
    """
    obj_path = Path(obj_path)

    vertices_x = []
    vertices_y = []
    vertices_z = []

    with open(obj_path, "r") as f:
        for line in f:
            if line.startswith("v "):
                parts = line.strip().split()
                if len(parts) >= 4:
                    try:
                        vertices_x.append(float(parts[1]))
                        vertices_y.append(float(parts[2]))
                        vertices_z.append(float(parts[3]))
                    except ValueError:
                        continue

    if not vertices_x:
        logger.warning(f"[MODEL] No vertices found in {obj_path.name}")
        return ModelOffset()

    vx = np.array(vertices_x, dtype=np.float64)
    vy = np.array(vertices_y, dtype=np.float64)
    vz = np.array(vertices_z, dtype=np.float64)

    offset = ModelOffset(
        x=float(np.mean(vx)),
        y=float(np.mean(vy)),
        z=float(np.mean(vz)),
        vertex_count=len(vertices_x),
        bbox_min=[float(np.min(vx)), float(np.min(vy)), float(np.min(vz))],
        bbox_max=[float(np.max(vx)), float(np.max(vy)), float(np.max(vz))],
    )

    logger.info(
        f"[MODEL] Offset from {obj_path.name}: "
        f"centroid=({offset.x:.6f}, {offset.y:.6f}, {offset.z:.6f}), "
        f"vertices={offset.vertex_count}, "
        f"bbox=[{offset.bbox_min}, {offset.bbox_max}]"
    )
    return offset


def convert_obj_to_gltf(
    obj_path: str | Path,
    output_path: str | Path | None = None,
    binary: bool = True,
) -> Path:
    """
    Convert a Wavefront OBJ model to glTF/glb format for web rendering.

    Attempts multiple conversion strategies in order of preference:
    1. trimesh (Python library — most portable)
    2. obj2gltf (Node.js CLI tool — high quality)
    3. Raw OBJ passthrough (fallback — served as-is)

    The glb (binary glTF) format is preferred because:
    - Single file (mesh + textures embedded)
    - Compact binary encoding
    - Native WebGL loading via Three.js / MapLibre
    - No CORS issues with external texture URLs

    Args:
        obj_path: Path to the input OBJ file
        output_path: Output path (defaults to same name with .glb extension)
        binary: If True, output glb (binary). If False, output gltf (JSON).

    Returns:
        Path to the converted glTF/glb file
    """
    obj_path = Path(obj_path)
    ext = ".glb" if binary else ".gltf"

    if output_path is None:
        output_path = obj_path.parent / f"{obj_path.stem}{ext}"
    output_path = Path(output_path)

    logger.info(f"[MODEL] Converting {obj_path.name} → {output_path.name}")

    # ── Strategy 1: trimesh ───────────────────────────────────────────────
    try:
        import trimesh

        logger.info("[MODEL] Using trimesh for conversion")

        # Load with resolver for MTL and textures in the same directory
        scene = trimesh.load(
            str(obj_path),
            file_type="obj",
            force="scene",
            resolver=trimesh.visual.resolvers.FilePathResolver(
                str(obj_path.parent)
            ),
        )

        if binary:
            # Export as binary GLB
            glb_data = scene.export(file_type="glb")
            with open(output_path, "wb") as f:
                f.write(glb_data)
        else:
            # Export as JSON glTF (with embedded buffers)
            gltf_data = scene.export(file_type="gltf")
            with open(output_path, "wb") as f:
                f.write(gltf_data)

        file_size = output_path.stat().st_size
        logger.info(
            f"[MODEL] trimesh conversion complete: "
            f"{file_size / 1024 / 1024:.1f} MB"
        )
        return output_path

    except ImportError:
        logger.info("[MODEL] trimesh not available, trying obj2gltf...")
    except Exception as e:
        logger.warning(f"[MODEL] trimesh conversion failed: {e}. Trying obj2gltf...")

    # ── Strategy 2: obj2gltf (Node.js) ────────────────────────────────────
    try:
        cmd = [
            "npx", "-y", "obj2gltf",
            "-i", str(obj_path),
            "-o", str(output_path),
        ]
        if binary:
            cmd.append("-b")

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300
        )

        if result.returncode == 0 and output_path.exists():
            file_size = output_path.stat().st_size
            logger.info(
                f"[MODEL] obj2gltf conversion complete: "
                f"{file_size / 1024 / 1024:.1f} MB"
            )
            return output_path
        else:
            logger.warning(f"[MODEL] obj2gltf failed: {result.stderr}")
    except FileNotFoundError:
        logger.info("[MODEL] obj2gltf not available")
    except Exception as e:
        logger.warning(f"[MODEL] obj2gltf conversion failed: {e}")

    # ── Strategy 3: Passthrough (serve OBJ as-is) ─────────────────────────
    # Copy OBJ to output location with a marker extension
    fallback_path = output_path.parent / f"{obj_path.stem}.obj"
    if fallback_path != obj_path:
        shutil.copy2(obj_path, fallback_path)

    logger.warning(
        f"[MODEL] No converter available. Serving raw OBJ: {fallback_path.name}"
    )
    return fallback_path


def get_model_metadata(
    model_path: str | Path,
    offset: ModelOffset | None = None,
) -> ModelMetadata:
    """
    Extract metadata from a 3D model file (glTF/glb/OBJ).

    Provides vertex counts, triangle counts, material information,
    and file size for database storage and frontend display.

    Args:
        model_path: Path to the model file
        offset: Pre-computed offset (if available)

    Returns:
        ModelMetadata with comprehensive model information
    """
    model_path = Path(model_path)
    file_size = model_path.stat().st_size if model_path.exists() else 0

    vertex_count = 0
    triangle_count = 0
    material_count = 0
    texture_count = 0
    bbox_local = {"min": [0.0, 0.0, 0.0], "max": [0.0, 0.0, 0.0]}
    fmt = model_path.suffix.lstrip(".") or "unknown"

    # Try to extract geometry stats
    if model_path.suffix.lower() == ".obj":
        # Parse OBJ for quick stats
        with open(model_path, "r") as f:
            for line in f:
                if line.startswith("v "):
                    vertex_count += 1
                elif line.startswith("f "):
                    triangle_count += 1
                elif line.startswith("usemtl "):
                    material_count += 1

    elif model_path.suffix.lower() in (".glb", ".gltf"):
        try:
            import trimesh
            scene = trimesh.load(str(model_path), force="scene")
            for geom in scene.geometry.values():
                vertex_count += len(geom.vertices)
                triangle_count += len(geom.faces)
            material_count = len(scene.geometry)
        except Exception:
            # If trimesh fails, just report file size
            pass

    if offset:
        bbox_local = {
            "min": offset.bbox_min,
            "max": offset.bbox_max,
        }

    meta = ModelMetadata(
        vertex_count=vertex_count,
        triangle_count=triangle_count,
        material_count=material_count,
        texture_count=texture_count,
        file_size_bytes=file_size,
        format=fmt,
        bbox_local=bbox_local,
        offset=offset,
    )

    logger.info(
        f"[MODEL-META] {model_path.name}: "
        f"{vertex_count:,} vertices, {triangle_count:,} triangles, "
        f"{file_size / 1024 / 1024:.1f} MB ({fmt})"
    )
    return meta
