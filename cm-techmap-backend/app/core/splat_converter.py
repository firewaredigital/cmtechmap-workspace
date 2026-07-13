"""
CM TECHMAP — 3D Gaussian Splatting Converter (PLY → .splat)

Converts photogrammetric point cloud data (PLY format from SfM/MVS)
into the optimized binary .splat format for real-time WebGL rendering.

Architecture context from research transcript (Cap. 4, Int. 3):
  - 3DGS represents environments with millions of ellipsoidal primitives
  - Each "Splat" carries: position (μ), covariance (Σ), opacity (α), SH coefficients
  - Trained via Stochastic Gradient Descent from multi-view images
  - Rendered via tile-based splatting for real-time performance
  - Surpasses traditional meshes for transparency, reflections, fine structures

Binary .splat format specification (per-splat, 32 bytes):
  Bytes 0-3:   position.x    (float32)
  Bytes 4-7:   position.y    (float32)
  Bytes 8-11:  position.z    (float32)
  Bytes 12-15: scale.x       (float32) — log-space
  Bytes 16-19: scale.y       (float32) — log-space
  Bytes 20-23: scale.z       (float32) — log-space
  Bytes 24:    color.r        (uint8)  — SH DC term mapped to [0,255]
  Bytes 25:    color.g        (uint8)
  Bytes 26:    color.b        (uint8)
  Bytes 27:    opacity        (uint8)  — sigmoid mapped to [0,255]
  Bytes 28-31: quaternion     (4×uint8) — rotation as normalized quaternion

This module handles:
  1. PLY parsing with SH coefficient extraction
  2. Covariance matrix → scale + quaternion decomposition
  3. SH DC band → RGB color conversion
  4. Sigmoid opacity → uint8 mapping
  5. Binary .splat file encoding for GPU streaming
  6. Offset/centroid extraction for RTE rendering
"""

import logging
import math
import os
import struct
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Constants
# ══════════════════════════════════════════════════════════════════════════════

# SH coefficient 0 (DC band) to RGB conversion factor
# C0 = 0.28209479177387814 (1 / (2 * sqrt(π)))
SH_C0 = 0.28209479177387814

# Sigmoid function for opacity conversion
def _sigmoid(x: float) -> float:
    """Numerically stable sigmoid activation."""
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    else:
        exp_x = math.exp(x)
        return exp_x / (1.0 + exp_x)


# ══════════════════════════════════════════════════════════════════════════════
# Data Structures
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class SplatMetadata:
    """Metadata about a converted Gaussian Splat asset."""
    splat_count: int = 0
    file_size_bytes: int = 0
    format: str = "splat"
    bbox_min: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    bbox_max: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    centroid: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    source_format: str = "ply"
    has_sh_coefficients: bool = False
    sh_degree: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SplatOffset:
    """Barycentric offset for RTE rendering of splat data."""
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    crs: str = "EPSG:4326"
    epsg: int = 4326
    splat_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ══════════════════════════════════════════════════════════════════════════════
# PLY Parser
# ══════════════════════════════════════════════════════════════════════════════

def _parse_ply_header(file_path: str) -> tuple[dict, int, str]:
    """
    Parse PLY header to extract property definitions and vertex count.

    Returns:
        (properties_dict, vertex_count, data_format)
        properties_dict maps property name → (dtype, byte_offset)
    """
    properties: list[tuple[str, str]] = []
    vertex_count = 0
    data_format = "binary_little_endian"
    header_bytes = 0

    with open(file_path, "rb") as f:
        while True:
            line = f.readline()
            header_bytes += len(line)
            line_str = line.decode("ascii", errors="replace").strip()

            if line_str.startswith("format"):
                parts = line_str.split()
                if len(parts) >= 2:
                    data_format = parts[1]

            elif line_str.startswith("element vertex"):
                parts = line_str.split()
                vertex_count = int(parts[2])

            elif line_str.startswith("property"):
                parts = line_str.split()
                if len(parts) >= 3:
                    prop_type = parts[1]
                    prop_name = parts[2]
                    properties.append((prop_name, prop_type))

            elif line_str == "end_header":
                break

    # Build property map with byte offsets
    type_sizes = {
        "float": 4, "float32": 4, "double": 8, "float64": 8,
        "uchar": 1, "uint8": 1, "char": 1, "int8": 1,
        "ushort": 2, "uint16": 2, "short": 2, "int16": 2,
        "uint": 4, "uint32": 4, "int": 4, "int32": 4,
    }

    numpy_types = {
        "float": np.float32, "float32": np.float32,
        "double": np.float64, "float64": np.float64,
        "uchar": np.uint8, "uint8": np.uint8,
        "char": np.int8, "int8": np.int8,
        "ushort": np.uint16, "uint16": np.uint16,
        "short": np.int16, "int16": np.int16,
        "uint": np.uint32, "uint32": np.uint32,
        "int": np.int32, "int32": np.int32,
    }

    prop_map = {}
    byte_offset = 0
    for name, ptype in properties:
        size = type_sizes.get(ptype, 4)
        np_type = numpy_types.get(ptype, np.float32)
        prop_map[name] = {
            "type": ptype,
            "np_type": np_type,
            "offset": byte_offset,
            "size": size,
        }
        byte_offset += size

    return prop_map, vertex_count, data_format


def _read_ply_vertices(
    file_path: str,
    prop_map: dict,
    vertex_count: int,
) -> dict[str, np.ndarray]:
    """
    Read binary PLY vertex data into numpy arrays.

    Efficiently reads only the properties needed for splat conversion:
    - Position: x, y, z
    - Scale: scale_0, scale_1, scale_2
    - Rotation quaternion: rot_0, rot_1, rot_2, rot_3
    - Opacity: opacity
    - SH coefficients: f_dc_0, f_dc_1, f_dc_2 (DC band for color)
    """
    # Calculate stride (bytes per vertex)
    stride = sum(p["size"] for p in prop_map.values())

    # Find header end position
    header_end = 0
    with open(file_path, "rb") as f:
        while True:
            line = f.readline()
            header_end += len(line)
            if b"end_header" in line:
                break

    # Read all vertex data at once
    with open(file_path, "rb") as f:
        f.seek(header_end)
        raw_data = f.read(stride * vertex_count)

    if len(raw_data) < stride * vertex_count:
        logger.warning(
            f"[SPLAT] PLY data truncated: expected {stride * vertex_count} "
            f"bytes, got {len(raw_data)}"
        )
        vertex_count = len(raw_data) // stride

    # Extract each property as a numpy array
    result = {}
    wanted_props = [
        "x", "y", "z",
        "scale_0", "scale_1", "scale_2",
        "rot_0", "rot_1", "rot_2", "rot_3",
        "opacity",
        "f_dc_0", "f_dc_1", "f_dc_2",
        # Higher SH bands (optional)
        "f_rest_0", "f_rest_1", "f_rest_2",
    ]

    raw_array = np.frombuffer(raw_data, dtype=np.uint8)

    for prop_name in wanted_props:
        if prop_name not in prop_map:
            continue
        info = prop_map[prop_name]
        offset = info["offset"]
        np_type = info["np_type"]
        size = info["size"]

        # Extract bytes for this property across all vertices
        indices = np.arange(vertex_count) * stride + offset
        prop_bytes = np.zeros(vertex_count * size, dtype=np.uint8)
        for byte_idx in range(size):
            prop_bytes[byte_idx::size] = raw_array[indices + byte_idx]

        result[prop_name] = prop_bytes.view(np_type)[:vertex_count].copy()

    return result


# ══════════════════════════════════════════════════════════════════════════════
# Splat Conversion Core
# ══════════════════════════════════════════════════════════════════════════════

def convert_ply_to_splat(
    ply_path: str | Path,
    output_path: str | Path | None = None,
    center_to_origin: bool = True,
) -> tuple[Path, SplatMetadata, SplatOffset]:
    """
    Convert a Gaussian Splatting PLY file to the binary .splat format.

    This is the core conversion implementing the architecture from the
    research transcript (Cap. 4): each Gaussian primitive is encoded as
    a compact 32-byte binary record optimized for GPU streaming.

    The conversion pipeline:
    1. Parse PLY header to identify property layout
    2. Read binary vertex data into numpy arrays
    3. Extract position (μ), convert SH DC → RGB, compute scale/rotation
    4. Apply barycentric centering (subtract centroid for RTE rendering)
    5. Encode to 32-byte binary records
    6. Write .splat file

    Args:
        ply_path: Path to the input PLY file (3DGS output)
        output_path: Output .splat file path (defaults to same stem)
        center_to_origin: If True, subtract centroid for RTE compatibility

    Returns:
        (output_path, metadata, offset) tuple
    """
    ply_path = Path(ply_path)

    if output_path is None:
        output_path = ply_path.parent / f"{ply_path.stem}.splat"
    output_path = Path(output_path)

    logger.info(f"[SPLAT] Converting {ply_path.name} → {output_path.name}")

    # ── Step 1: Parse PLY header ──────────────────────────────────────
    prop_map, vertex_count, data_format = _parse_ply_header(str(ply_path))

    if vertex_count == 0:
        raise ValueError(f"PLY file has no vertices: {ply_path}")

    logger.info(
        f"[SPLAT] PLY header: {vertex_count:,} vertices, "
        f"{len(prop_map)} properties, format={data_format}"
    )

    # Validate required properties
    required = {"x", "y", "z"}
    missing = required - set(prop_map.keys())
    if missing:
        raise ValueError(f"PLY missing required properties: {missing}")

    # ── Step 2: Read vertex data ──────────────────────────────────────
    data = _read_ply_vertices(str(ply_path), prop_map, vertex_count)

    positions_x = data.get("x", np.zeros(vertex_count, dtype=np.float32))
    positions_y = data.get("y", np.zeros(vertex_count, dtype=np.float32))
    positions_z = data.get("z", np.zeros(vertex_count, dtype=np.float32))

    # ── Step 3: Compute centroid and offset ────────────────────────────
    centroid_x = float(np.mean(positions_x.astype(np.float64)))
    centroid_y = float(np.mean(positions_y.astype(np.float64)))
    centroid_z = float(np.mean(positions_z.astype(np.float64)))

    offset = SplatOffset(
        x=centroid_x,
        y=centroid_y,
        z=centroid_z,
        splat_count=vertex_count,
    )

    # Center positions for RTE rendering (Cap. 5)
    if center_to_origin:
        positions_x = positions_x - np.float32(centroid_x)
        positions_y = positions_y - np.float32(centroid_y)
        positions_z = positions_z - np.float32(centroid_z)
        logger.info(
            f"[SPLAT] Centered to origin. Offset: "
            f"({centroid_x:.6f}, {centroid_y:.6f}, {centroid_z:.6f})"
        )

    # ── Step 4: Extract scale (log-space) ─────────────────────────────
    scale_x = data.get("scale_0", np.zeros(vertex_count, dtype=np.float32))
    scale_y = data.get("scale_1", np.zeros(vertex_count, dtype=np.float32))
    scale_z = data.get("scale_2", np.zeros(vertex_count, dtype=np.float32))

    # ── Step 5: Extract rotation quaternion ────────────────────────────
    rot_0 = data.get("rot_0", np.ones(vertex_count, dtype=np.float32))
    rot_1 = data.get("rot_1", np.zeros(vertex_count, dtype=np.float32))
    rot_2 = data.get("rot_2", np.zeros(vertex_count, dtype=np.float32))
    rot_3 = data.get("rot_3", np.zeros(vertex_count, dtype=np.float32))

    # Normalize quaternions
    quat_norm = np.sqrt(rot_0**2 + rot_1**2 + rot_2**2 + rot_3**2)
    quat_norm = np.maximum(quat_norm, 1e-10)
    rot_0 /= quat_norm
    rot_1 /= quat_norm
    rot_2 /= quat_norm
    rot_3 /= quat_norm

    # ── Step 6: Convert SH DC band → RGB color ────────────────────────
    # From research transcript (Cap. 4): Spherical Harmonics encode
    # view-dependent color. The DC (0th order) band gives the base color.
    # color = SH_C0 * f_dc + 0.5 (normalized to [0,1])
    has_sh = "f_dc_0" in data
    sh_degree = 0

    if has_sh:
        sh_degree = 1 if "f_rest_0" in data else 0
        f_dc_0 = data["f_dc_0"]
        f_dc_1 = data["f_dc_1"]
        f_dc_2 = data["f_dc_2"]

        color_r = np.clip((SH_C0 * f_dc_0 + 0.5) * 255, 0, 255).astype(np.uint8)
        color_g = np.clip((SH_C0 * f_dc_1 + 0.5) * 255, 0, 255).astype(np.uint8)
        color_b = np.clip((SH_C0 * f_dc_2 + 0.5) * 255, 0, 255).astype(np.uint8)
    else:
        # Fallback: neutral gray
        color_r = np.full(vertex_count, 180, dtype=np.uint8)
        color_g = np.full(vertex_count, 180, dtype=np.uint8)
        color_b = np.full(vertex_count, 180, dtype=np.uint8)

    # ── Step 7: Convert opacity (logit → sigmoid → uint8) ─────────────
    if "opacity" in data:
        raw_opacity = data["opacity"]
        # Apply sigmoid activation then map to [0, 255]
        sigmoid_opacity = 1.0 / (1.0 + np.exp(-raw_opacity.astype(np.float64)))
        opacity_u8 = np.clip(sigmoid_opacity * 255, 0, 255).astype(np.uint8)
    else:
        opacity_u8 = np.full(vertex_count, 255, dtype=np.uint8)

    # ── Step 8: Encode quaternion to uint8 ─────────────────────────────
    # Map quaternion components from [-1, 1] → [0, 255]
    quat_r = np.clip((rot_0 * 128 + 128), 0, 255).astype(np.uint8)
    quat_i = np.clip((rot_1 * 128 + 128), 0, 255).astype(np.uint8)
    quat_j = np.clip((rot_2 * 128 + 128), 0, 255).astype(np.uint8)
    quat_k = np.clip((rot_3 * 128 + 128), 0, 255).astype(np.uint8)

    # ── Step 9: Write binary .splat file ──────────────────────────────
    # Format: 32 bytes per splat
    # [pos_x:f32][pos_y:f32][pos_z:f32][scale_x:f32][scale_y:f32][scale_z:f32]
    # [r:u8][g:u8][b:u8][opacity:u8][qr:u8][qi:u8][qj:u8][qk:u8]
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "wb") as f:
        for i in range(vertex_count):
            # 6 × float32 = 24 bytes (position + scale)
            f.write(struct.pack("<fff",
                float(positions_x[i]),
                float(positions_y[i]),
                float(positions_z[i]),
            ))
            f.write(struct.pack("<fff",
                float(scale_x[i]),
                float(scale_y[i]),
                float(scale_z[i]),
            ))
            # 8 × uint8 = 8 bytes (color + opacity + quaternion)
            f.write(struct.pack("BBBBBBBB",
                int(color_r[i]),
                int(color_g[i]),
                int(color_b[i]),
                int(opacity_u8[i]),
                int(quat_r[i]),
                int(quat_i[i]),
                int(quat_j[i]),
                int(quat_k[i]),
            ))

    file_size = output_path.stat().st_size

    # ── Step 10: Compute metadata ─────────────────────────────────────
    metadata = SplatMetadata(
        splat_count=vertex_count,
        file_size_bytes=file_size,
        format="splat",
        bbox_min=[
            float(np.min(positions_x)),
            float(np.min(positions_y)),
            float(np.min(positions_z)),
        ],
        bbox_max=[
            float(np.max(positions_x)),
            float(np.max(positions_y)),
            float(np.max(positions_z)),
        ],
        centroid=[centroid_x, centroid_y, centroid_z],
        source_format="ply",
        has_sh_coefficients=has_sh,
        sh_degree=sh_degree,
    )

    logger.info(
        f"[SPLAT] Conversion complete: {vertex_count:,} splats, "
        f"{file_size / 1024 / 1024:.1f} MB, "
        f"SH={'yes' if has_sh else 'no'} (degree {sh_degree})"
    )

    return output_path, metadata, offset


def convert_pointcloud_to_splat(
    input_path: str | Path,
    output_path: str | Path | None = None,
) -> tuple[Path, SplatMetadata, SplatOffset]:
    """
    Convert a generic point cloud (PLY/LAS/LAZ) to .splat format.

    For non-Gaussian PLY files (plain point clouds from SfM), this
    creates synthetic Gaussian splats with:
    - Position from point coordinates
    - Color from vertex colors (if available) or neutral gray
    - Uniform scale (small spheres)
    - Identity rotation
    - Full opacity

    This is a fallback for when true 3DGS training hasn't been performed,
    allowing the frontend to still render a point-cloud-like visualization
    using the same splatting renderer.

    Args:
        input_path: Path to PLY/LAS/LAZ file
        output_path: Output .splat path

    Returns:
        (output_path, metadata, offset) tuple
    """
    input_path = Path(input_path)

    if input_path.suffix.lower() in (".ply",):
        return convert_ply_to_splat(input_path, output_path)

    # For LAS/LAZ, use laspy to read and create synthetic splats
    if input_path.suffix.lower() in (".las", ".laz"):
        return _convert_las_to_splat(input_path, output_path)

    raise ValueError(f"Unsupported point cloud format: {input_path.suffix}")


def _convert_las_to_splat(
    las_path: Path,
    output_path: Path | None = None,
) -> tuple[Path, SplatMetadata, SplatOffset]:
    """
    Convert LAS/LAZ point cloud to high-quality synthetic .splat format.

    Instead of uniform-sized point splats, this implementation estimates
    Gaussian parameters from the local geometry of the point cloud:

    1. **Scale (KNN)**: Splat size = mean distance to K nearest neighbors.
       Dense areas get smaller splats; sparse areas get larger ones.
       This creates a continuous surface appearance.

    2. **Rotation (PCA)**: Fits a local tangent plane via PCA of the K
       nearest neighbors. The splat is oriented to lie flat on the
       estimated surface, creating proper ellipsoidal coverage.

    3. **Opacity (density)**: Points in dense regions get higher opacity;
       isolated points get lower opacity to reduce noise artifacts.
    """
    if output_path is None:
        output_path = las_path.parent / f"{las_path.stem}.splat"
    output_path = Path(output_path)

    try:
        import laspy
    except ImportError:
        raise ImportError(
            "laspy is required for LAS/LAZ conversion. "
            "Install with: pip install laspy[lazrs]"
        )

    logger.info(f"[SPLAT] Converting LAS {las_path.name} → {output_path.name}")

    las = laspy.read(str(las_path))
    n = len(las.points)

    # Stack positions for KNN queries
    positions = np.column_stack([
        las.x.astype(np.float64),
        las.y.astype(np.float64),
        las.z.astype(np.float64),
    ])

    # Compute centroid (Float64)
    centroid_x = float(np.mean(positions[:, 0]))
    centroid_y = float(np.mean(positions[:, 1]))
    centroid_z = float(np.mean(positions[:, 2]))

    # Center positions for RTE rendering
    positions[:, 0] -= centroid_x
    positions[:, 1] -= centroid_y
    positions[:, 2] -= centroid_z

    positions_f32 = positions.astype(np.float32)

    # ── Extract colors ────────────────────────────────────────────────
    try:
        color_r = (las.red / 256).astype(np.uint8)
        color_g = (las.green / 256).astype(np.uint8)
        color_b = (las.blue / 256).astype(np.uint8)
    except AttributeError:
        color_r = np.full(n, 180, dtype=np.uint8)
        color_g = np.full(n, 180, dtype=np.uint8)
        color_b = np.full(n, 180, dtype=np.uint8)

    # ── KNN-based scale + PCA-based rotation + density opacity ────────
    K = 8  # Number of nearest neighbors
    logger.info(f"[SPLAT] Computing KNN (K={K}) for {n:,} points...")

    # Use scipy KDTree for efficient spatial queries
    try:
        from scipy.spatial import cKDTree
        tree = cKDTree(positions_f32)
        distances, indices = tree.query(positions_f32, k=K + 1)
        # Exclude self (distance 0) — columns 1..K
        knn_distances = distances[:, 1:]  # shape (n, K)
        knn_indices = indices[:, 1:]      # shape (n, K)
    except ImportError:
        logger.warning("[SPLAT] scipy not available, using uniform scale fallback")
        # Fallback: uniform scale
        default_scale = np.float32(math.log(0.02))
        scale_x = np.full(n, default_scale, dtype=np.float32)
        scale_y = np.full(n, default_scale, dtype=np.float32)
        scale_z = np.full(n, default_scale, dtype=np.float32)
        opacity_u8 = np.full(n, 220, dtype=np.uint8)
        quat_r = np.full(n, 128, dtype=np.uint8)
        quat_i = np.full(n, 128, dtype=np.uint8)
        quat_j = np.full(n, 128, dtype=np.uint8)
        quat_k = np.full(n, 128, dtype=np.uint8)
        knn_distances = None
        knn_indices = None

    if knn_distances is not None and knn_indices is not None:
        # ── Scale from mean KNN distance ──────────────────────────
        # Mean distance to K neighbors → splat covers local area
        mean_dist = np.mean(knn_distances, axis=1).astype(np.float32)
        # Clamp to reasonable range (0.5cm to 2m)
        mean_dist = np.clip(mean_dist, 0.005, 2.0)
        # Log-space for the .splat format
        scale_x = np.log(mean_dist * 0.8).astype(np.float32)  # slightly smaller than gap
        scale_y = np.log(mean_dist * 0.8).astype(np.float32)
        # Z scale is thinner (splats lie on surface like pancakes)
        scale_z = np.log(mean_dist * 0.3).astype(np.float32)

        logger.info(
            f"[SPLAT] Scale range: [{np.exp(np.min(scale_x)):.4f}, "
            f"{np.exp(np.max(scale_x)):.4f}] meters"
        )

        # ── Opacity from local density ────────────────────────────
        # Dense regions → high opacity; sparse → lower
        # Density = 1 / mean_distance (normalized)
        density = 1.0 / (mean_dist + 1e-6)
        density_norm = (density - density.min()) / (density.max() - density.min() + 1e-10)
        # Map to [150, 255] range
        opacity_u8 = np.clip(150 + density_norm * 105, 150, 255).astype(np.uint8)

        # ── Rotation from PCA (local surface normal) ──────────────
        # For every point, compute PCA of its K neighbors to find
        # the local surface normal. The splat quaternion aligns
        # the splat's Z-axis with this normal (flat on surface).
        logger.info("[SPLAT] Computing PCA rotations for surface alignment...")

        quat_r = np.full(n, 128, dtype=np.uint8)
        quat_i = np.full(n, 128, dtype=np.uint8)
        quat_j = np.full(n, 128, dtype=np.uint8)
        quat_k = np.full(n, 128, dtype=np.uint8)

        # Process in batches for memory efficiency
        BATCH = 50000
        for batch_start in range(0, n, BATCH):
            batch_end = min(batch_start + BATCH, n)

            for i in range(batch_start, batch_end):
                neighbors = positions_f32[knn_indices[i]]
                # Center neighbors
                centered = neighbors - neighbors.mean(axis=0)

                # SVD for PCA (3×K matrix → 3 eigenvalues)
                try:
                    _, s, vh = np.linalg.svd(centered, full_matrices=False)
                    # Normal = last right singular vector (smallest eigenvalue)
                    normal = vh[2]  # shape (3,)

                    # Make normal point upward (Z positive)
                    if normal[2] < 0:
                        normal = -normal

                    # Convert normal to quaternion
                    # Rotation from Z-axis [0,0,1] to normal
                    up = np.array([0, 0, 1], dtype=np.float32)
                    dot = np.clip(np.dot(up, normal), -1, 1)

                    if dot > 0.9999:
                        # Already aligned — identity quaternion
                        pass
                    elif dot < -0.9999:
                        # 180° rotation
                        quat_r[i] = 0
                        quat_i[i] = 255  # [0, 1, 0, 0] mapped
                    else:
                        cross = np.cross(up, normal)
                        w = 1.0 + dot
                        q = np.array([w, cross[0], cross[1], cross[2]])
                        q /= np.linalg.norm(q)
                        # Map [-1,1] → [0,255]
                        quat_r[i] = np.clip(int(q[0] * 128 + 128), 0, 255)
                        quat_i[i] = np.clip(int(q[1] * 128 + 128), 0, 255)
                        quat_j[i] = np.clip(int(q[2] * 128 + 128), 0, 255)
                        quat_k[i] = np.clip(int(q[3] * 128 + 128), 0, 255)
                except np.linalg.LinAlgError:
                    pass  # Keep identity quaternion

            if batch_end < n:
                logger.info(f"[SPLAT] PCA progress: {batch_end:,}/{n:,}")

    # ── Vectorized binary write (much faster than per-point loop) ─────
    logger.info("[SPLAT] Writing binary .splat file...")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Build the entire buffer at once using numpy
    buf = np.empty(n * 32, dtype=np.uint8)

    # Positions (3×f32 = 12 bytes)
    pos_bytes = positions_f32.tobytes()
    for i in range(n):
        buf[i * 32: i * 32 + 12] = np.frombuffer(
            positions_f32[i].tobytes(), dtype=np.uint8
        )

    # Use struct pack_into with a bytearray for maximum speed
    import array
    out_buf = bytearray(n * 32)
    for i in range(n):
        offset_b = i * 32
        struct.pack_into("<fff", out_buf, offset_b,
            float(positions_f32[i, 0]),
            float(positions_f32[i, 1]),
            float(positions_f32[i, 2]),
        )
        struct.pack_into("<fff", out_buf, offset_b + 12,
            float(scale_x[i]),
            float(scale_y[i]),
            float(scale_z[i]),
        )
        struct.pack_into("BBBBBBBB", out_buf, offset_b + 24,
            int(color_r[i]), int(color_g[i]), int(color_b[i]),
            int(opacity_u8[i]),
            int(quat_r[i]), int(quat_i[i]), int(quat_j[i]), int(quat_k[i]),
        )

    with open(output_path, "wb") as f:
        f.write(out_buf)

    file_size = output_path.stat().st_size

    offset = SplatOffset(
        x=centroid_x, y=centroid_y, z=centroid_z,
        splat_count=n,
    )

    metadata = SplatMetadata(
        splat_count=n,
        file_size_bytes=file_size,
        format="splat",
        bbox_min=[float(np.min(positions_x)), float(np.min(positions_y)), float(np.min(positions_z))],
        bbox_max=[float(np.max(positions_x)), float(np.max(positions_y)), float(np.max(positions_z))],
        centroid=[centroid_x, centroid_y, centroid_z],
        source_format="las",
        has_sh_coefficients=False,
        sh_degree=0,
    )

    logger.info(f"[SPLAT] LAS conversion: {n:,} points → {file_size / 1024 / 1024:.1f} MB")
    return output_path, metadata, offset
