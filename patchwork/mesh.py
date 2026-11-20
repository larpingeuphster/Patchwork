"""Read Roblox mesh files and draw them, for the Cache tab's 3D previews.

Supports the text format (1.00/1.01), the binary formats 2.00-5.00 and the
chunked 6.00/7.00 format, including Draco-compressed COREMESH chunks (decoded with
DracoPy, Google's Draco library). Only the first (most detailed) level of detail is used.
"""

from __future__ import annotations

import io
import math
import re
import struct
from dataclasses import dataclass


class MeshError(ValueError):
    pass


@dataclass
class Mesh:
    version: str
    positions: list[tuple[float, float, float]]
    faces: list[tuple[int, int, int]]

    def describe(self) -> str:
        return f"mesh {self.version}, {len(self.positions)} vertices, {len(self.faces)} triangles"


def _lod_end(data: bytes, lod_offset: int, num_lods: int, num_faces: int) -> int:
    if num_lods >= 2 and lod_offset + 8 <= len(data):
        _first, second = struct.unpack_from("<2I", data, lod_offset)
        if 0 < second <= num_faces:
            return second
    return num_faces


def _read_verts(data: bytes, off: int, count: int, size: int) -> list[tuple[float, float, float]]:
    if off + count * size > len(data):
        raise MeshError("truncated vertex data")
    return [struct.unpack_from("<3f", data, off + i * size) for i in range(count)]


def _read_faces(data: bytes, off: int, count: int, nverts: int) -> list[tuple[int, int, int]]:
    if off + count * 12 > len(data):
        raise MeshError("truncated face data")
    out = []
    for i in range(count):
        a, b, c = struct.unpack_from("<3I", data, off + i * 12)
        if a < nverts and b < nverts and c < nverts:
            out.append((a, b, c))
    return out


def _draco(chunk: bytes) -> tuple[list, list]:
    """A Draco-compressed COREMESH (Roblox's newer meshes): positions and triangles."""
    at = chunk.find(b"DRACO")
    if at < 0:
        raise MeshError("this mesh is compressed in a way Patchwork doesn't recognise")
    try:
        import DracoPy
    except ImportError as exc:
        raise MeshError("this mesh is compressed (Draco) and the decoder isn't in this build") from exc
    try:
        decoded = DracoPy.decode(bytes(chunk[at:]))
    except Exception as exc:
        raise MeshError(f"couldn't decompress this mesh ({exc})") from exc
    positions = [tuple(map(float, p)) for p in decoded.points]
    faces_arr = getattr(decoded, "faces", None)
    faces = [] if faces_arr is None else [tuple(int(i) for i in f) for f in faces_arr]
    return positions, faces


def parse(data: bytes) -> Mesh:
    if not data.startswith(b"version "):
        raise MeshError("not a Roblox mesh")
    nl = data.find(b"\n")
    version = data[8:nl].strip().decode("ascii", "replace")
    body = nl + 1
    major = version.split(".")[0]

    if major == "1":
        text = data[body:].decode("ascii", "replace")
        lines = text.splitlines()
        nums = re.findall(r"\[([^\]]*)\]", lines[1] if len(lines) > 1 else "")
        positions = []
        scale = 0.5 if version == "1.00" else 1.0
        for i in range(0, len(nums) - 2, 3):
            x, y, z = (float(v) * scale for v in nums[i].split(","))
            positions.append((x, y, z))
        faces = [(i, i + 1, i + 2) for i in range(0, len(positions) - 2, 3)]
        return Mesh(version, positions, faces)

    if major in ("2", "3"):
        header_size, vsize, _fsize = struct.unpack_from("<HBB", data, body)
        if major == "2":
            nverts, nfaces = struct.unpack_from("<2I", data, body + 4)
            num_lods = 0
        else:
            _lod_size, num_lods, nverts, nfaces = struct.unpack_from("<2H2I", data, body + 4)
        off = body + header_size
        positions = _read_verts(data, off, nverts, vsize)
        off += nverts * vsize
        end = _lod_end(data, off + nfaces * 12, num_lods, nfaces)
        return Mesh(version, positions, _read_faces(data, off, end, nverts))

    if major in ("4", "5"):
        header_size, _lod_type, nverts, nfaces, num_lods, num_bones = struct.unpack_from("<2H2I2H", data, body)
        off = body + header_size
        positions = _read_verts(data, off, nverts, 40)
        off += nverts * 40
        if num_bones:
            off += nverts * 8  # skinning envelopes
        end = _lod_end(data, off + nfaces * 12, num_lods, nfaces)
        return Mesh(version, positions, _read_faces(data, off, end, nverts))

    if major in ("6", "7"):
        off = body
        positions: list = []
        faces: list = []
        num_faces = 0
        lod_end = None
        while off + 16 <= len(data):
            kind = data[off:off + 8].rstrip(b"\0").decode("ascii", "replace")
            chunk_version, size = struct.unpack_from("<2I", data, off + 8)
            start = off + 16
            if kind == "COREMESH":
                if chunk_version != 1:
                    positions, faces = _draco(data[start:start + size])
                    off = start + size
                    continue
                nverts = struct.unpack_from("<I", data, start)[0]
                positions = _read_verts(data, start + 4, nverts, 40)
                foff = start + 4 + nverts * 40
                num_faces = struct.unpack_from("<I", data, foff)[0]
                faces = _read_faces(data, foff + 4, num_faces, nverts)
            elif kind == "LODS" and size >= 7:
                _t, _hq, num_lods = struct.unpack_from("<HBI", data, start)
                if num_lods >= 2:
                    lod_end = struct.unpack_from("<I", data, start + 7 + 4)[0]
            off = start + size
        if not positions:
            raise MeshError("no mesh data found")
        if lod_end and 0 < lod_end <= len(faces):
            faces = faces[:lod_end]
        return Mesh(version, positions, faces)

    raise MeshError(f"mesh version {version} isn't supported")


# ---------------------------------------------------------------------------------------------
# Wavefront OBJ (what skin makers export from Blender) -> Roblox mesh


def is_obj(data: bytes) -> bool:
    head = data[:4096].decode("utf-8", "replace")
    return any(line.startswith("v ") for line in head.splitlines()) and not data.startswith(b"version ")


def from_obj(data: bytes) -> bytes:
    """Convert an OBJ model to a Roblox mesh (binary 2.00: position, normal, UV per vertex).

    Polygons are split into triangles; missing normals are computed. Refuses models that
    are empty or flat, so a swap can't make a 3D object disappear.
    """
    vs: list[tuple[float, float, float]] = []
    vts: list[tuple[float, float]] = []
    vns: list[tuple[float, float, float]] = []
    corners: dict[tuple[int, int, int], int] = {}
    out_v: list[tuple] = []
    faces: list[tuple[int, int, int]] = []

    def index(token: str) -> int:
        parts = (token.split("/") + ["", ""])[:3]
        vi = int(parts[0])
        ti = int(parts[1]) if parts[1] else 0
        ni = int(parts[2]) if parts[2] else 0
        vi = vi - 1 if vi > 0 else len(vs) + vi
        ti = (ti - 1 if ti > 0 else len(vts) + ti) if ti else -1
        ni = (ni - 1 if ni > 0 else len(vns) + ni) if ni else -1
        key = (vi, ti, ni)
        if key not in corners:
            pos = vs[vi]
            uv = vts[ti] if 0 <= ti < len(vts) else (0.0, 0.0)
            nrm = vns[ni] if 0 <= ni < len(vns) else None
            corners[key] = len(out_v)
            out_v.append((pos, nrm, uv))
        return corners[key]

    for raw in data.decode("utf-8", "replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        tag, _, rest = line.partition(" ")
        nums = rest.split()
        try:
            if tag == "v":
                vs.append((float(nums[0]), float(nums[1]), float(nums[2])))
            elif tag == "vt":
                vts.append((float(nums[0]), float(nums[1]) if len(nums) > 1 else 0.0))
            elif tag == "vn":
                vns.append((float(nums[0]), float(nums[1]), float(nums[2])))
            elif tag == "f" and len(nums) >= 3:
                ids = [index(t) for t in nums]
                for k in range(1, len(ids) - 1):
                    faces.append((ids[0], ids[k], ids[k + 1]))
        except (ValueError, IndexError) as exc:
            raise MeshError(f"the OBJ file has a line Patchwork can't read: {line[:60]!r}") from exc

    if len(faces) < 4:
        raise MeshError("the OBJ model is empty")
    xs, ys, zs = zip(*(v[0] for v in out_v))
    ext = (max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))
    if min(ext) <= max(ext) * 0.005:
        raise MeshError("the OBJ model is flat, which could make the object vanish from some angles")

    # Fill in normals the file didn't give: average of the faces around each vertex.
    acc = [[0.0, 0.0, 0.0] for _ in out_v]
    for a, b, c in faces:
        (ax, ay, az), (bx, by, bz), (cx, cy, cz) = out_v[a][0], out_v[b][0], out_v[c][0]
        ux, uy, uz, wx, wy, wz = bx - ax, by - ay, bz - az, cx - ax, cy - ay, cz - az
        n = (uy * wz - uz * wy, uz * wx - ux * wz, ux * wy - uy * wx)
        for i in (a, b, c):
            acc[i][0] += n[0]
            acc[i][1] += n[1]
            acc[i][2] += n[2]
    verts = b""
    for i, (pos, nrm, uv) in enumerate(out_v):
        if nrm is None:
            ln = math.sqrt(sum(x * x for x in acc[i])) or 1.0
            nrm = tuple(x / ln for x in acc[i])
        verts += struct.pack("<3f3f3f", *pos, *nrm, uv[0], 1.0 - uv[1], 0.0)  # Roblox's V runs top-down
    tris = b"".join(struct.pack("<3I", *f) for f in faces)
    return b"version 2.00\n" + struct.pack("<HBB2I", 12, 36, 12, len(out_v), len(faces)) + verts + tris


# ---------------------------------------------------------------------------------------------
# Recognising a mesh by its shape


def shape(m: Mesh) -> tuple[int, float, float, float]:
    """(triangles, width, height, depth): survives Roblox re-encoding a mesh (e.g. as Draco)."""
    if not m.positions:
        return (0, 0.0, 0.0, 0.0)
    xs, ys, zs = zip(*m.positions)
    return (len(m.faces), max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))


def shape_distance(a, b) -> float:
    """0 for the same shape; grows with the relative difference in triangle count and size."""
    if not a or not b or not a[0] or not b[0]:
        return 1e9
    faces = abs(a[0] - b[0]) / max(a[0], b[0])
    size = max(abs(x - y) / max(abs(x), abs(y), 1e-6) for x, y in zip(a[1:], b[1:]))
    return faces + size


# ---------------------------------------------------------------------------------------------
# Drawing


def _normalised(mesh: Mesh) -> list[tuple[float, float, float]]:
    xs, ys, zs = zip(*mesh.positions) if mesh.positions else ((0,), (0,), (0,))
    cx, cy, cz = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2, (max(zs) + min(zs)) / 2
    r = max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)) / 2 or 1.0
    return [((x - cx) / r, (y - cy) / r, (z - cz) / r) for x, y, z in mesh.positions]


def decimate(mesh: Mesh, max_faces: int) -> list[tuple[int, int, int]]:
    if len(mesh.faces) <= max_faces:
        return mesh.faces
    step = len(mesh.faces) / max_faces
    return [mesh.faces[int(i * step)] for i in range(max_faces)]


def render(mesh: Mesh, size: int = 192, yaw: float = 35.0, pitch: float = 20.0, max_faces: int = 12000) -> bytes:
    """A PNG of the mesh, lit from the viewer's side, on a transparent background."""
    from PIL import Image, ImageDraw
    ss = 2
    w = size * ss
    pts = _normalised(mesh)
    cy, sy = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
    cp, sp = math.cos(math.radians(pitch)), math.sin(math.radians(pitch))
    view = []
    for x, y, z in pts:
        x1, z1 = x * cy + z * sy, -x * sy + z * cy
        y2, z2 = y * cp - z1 * sp, y * sp + z1 * cp
        view.append((x1, y2, z2))
    scale = w * 0.42
    proj = [(w / 2 + x * scale, w / 2 - y * scale) for x, y, _ in view]
    light = (0.35, 0.55, 0.76)
    tris = []
    for a, b, c in decimate(mesh, max_faces):
        (ax, ay, az), (bx, by, bz), (cx_, cy_, cz) = view[a], view[b], view[c]
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx_ - ax, cy_ - ay, cz - az
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        ln = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
        nx, ny, nz = nx / ln, ny / ln, nz / ln
        if nz > 0:  # facing away from the camera (Roblox winds faces counter-clockwise)
            nx, ny, nz = -nx, -ny, -nz
        shade = 0.25 + 0.75 * max(0.0, -(nx * light[0] + ny * light[1] - nz * light[2]))
        tris.append(((az + bz + cz) / 3, shade, (proj[a], proj[b], proj[c])))
    tris.sort(key=lambda t: t[0], reverse=True)  # far first (larger z is farther away)
    img = Image.new("RGBA", (w, w), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    for _, shade, poly in tris:
        col = (int(150 + 60 * shade), int(110 + 90 * shade), int(200 + 55 * shade), 255)
        col = tuple(min(255, int(c * (0.45 + 0.55 * shade))) for c in col[:3]) + (255,)
        draw.polygon(poly, fill=col)
    img = img.resize((size, size), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def for_viewer(mesh: Mesh, max_faces: int = 15000) -> dict:
    """Positions (normalised to -1..1) and triangles for the interactive viewer."""
    faces = decimate(mesh, max_faces)
    used = sorted({i for f in faces for i in f})
    remap = {old: new for new, old in enumerate(used)}
    pts = _normalised(mesh)
    flat = [round(c, 4) for i in used for c in pts[i]]
    return {"positions": flat, "faces": [remap[i] for f in faces for i in f]}


def make(version: str = "4.00", positions=None, faces=None) -> bytes:
    """A small mesh file in binary format 2.00 or 4.00 (for tests)."""
    positions = positions or [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)]
    faces = faces or [(0, 2, 1), (0, 1, 3), (0, 3, 2), (1, 2, 3)]
    verts = b"".join(struct.pack("<3f3f2f4b4B", *p, 0, 1, 0, 0, 0, 0, 0, 0, 0, 255, 255, 255, 255) for p in positions)
    tris = b"".join(struct.pack("<3I", *f) for f in faces)
    if version.startswith("2"):
        return b"version 2.00\n" + struct.pack("<HBB2I", 12, 40, 12, len(positions), len(faces)) + verts + tris
    header = struct.pack("<2H2I2HI H2B", 24, 0, len(positions), len(faces), 2, 0, 0, 0, 0, 0)
    lods = struct.pack("<2I", 0, len(faces))
    return f"version {version}\n".encode() + header + verts + tris + lods
