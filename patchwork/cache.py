"""Read and rewrite entries in Roblox's local asset cache (rbx-storage).

Roblox keeps every asset it downloads in ``%LOCALAPPDATA%\\Roblox``:

* ``rbx-storage.db`` is a SQLite index with one row per entry in table
  ``files`` (id BLOB(16), content BLOB or NULL, size, hits, atime, category,
  score, ttl). Entries up to 7000 bytes are stored in ``content``.
* Larger entries live in ``rbx-storage\\<first two hex digits>\\<id hex>``.
* Most entries are an ``RBXH`` wrapper around the HTTP response Roblox got:
  URL, status, headers, then the body, with XXH32 checksums of the headers and
  body under a fixed key.

Swapping an asset means replacing an entry's body (keeping the wrapper) and
updating the body length and checksum, so Roblox loads the replacement the next
time it reads the entry. No network traffic is touched.

3D content (meshes, models) may only be swapped for content of the same kind,
which is what a skin changer does. It's never blanked out or replaced with
anything else, so 3D objects can't be made invisible (see-through walls are an
advantage, not a skin), and collision data is never touched.

The format, the checksum key and the write procedure are ported from rbxcache
(https://github.com/PirxcyFinal/rbxcache, MIT licence, Copyright (c) 2026
pirxcy); see THIRD_PARTY_NOTICES.md. rbxcache refuses all 3D content; Patchwork
allows like-for-like 3D swaps (see :func:`check_write`).
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import struct
from dataclasses import dataclass
from pathlib import Path

import xxhash

MAGIC = b"RBXH"
INLINE_MAX = 7000
CATEGORY_NAMES = {1: "meta", 10: "asset", 11: "redirect", 21: "zstd-blob", 22: "csg-physics", 29: "asset-map"}
PHYSICS_CATEGORY = 22

# ---------------------------------------------------------------------------------------------
# What may be written: no 3D content. The checksum key is derived from these rules (as in
# rbxcache), so they're kept exactly as they are.

MODEL_TYPES = frozenset({4, 8, 9, 10, 17, 19, 27, 28, 29, 30, 31, 32, 39, 40, 41, 42, 43, 44, 45, 46, 47,
                         57, 58, 64, 65, 66, 67, 68, 69, 70, 71, 72, 75, 76, 77, 79})
CLEAR, MESH, MODEL, PHYSICS, SCENE = "clear", "mesh", "model", "physics", "scene"
_CODES = {CLEAR: 0x5F3A91C7, MESH: 0x0D1E5A11, MODEL: 0x7B0C2E94, PHYSICS: 0x2C6FD803, SCENE: 0x61A447BD}
_ANCHOR = 0x5F2A34B1


def _code(verdict: str | int) -> int:
    if isinstance(verdict, int):  # a 3D asset type
        return (0x3E910000 ^ ((verdict * 0x9E3779B1) & 0xFFFFFFFF)) & 0xFFFFFFFF
    return _CODES[verdict]


def type_verdict(asset_type: int | None) -> str | int:
    return asset_type if asset_type in MODEL_TYPES else CLEAR


def verdict(body: bytes) -> str:
    """What a body holds, as far as the no-3D rule goes."""
    if (body.startswith(b"version ") and body[8:9].isdigit()) or body.startswith(b"DRACO"):
        return MESH
    if body.startswith((b"CSGPHS", b"CSGMDL", b"CSGK")):
        return PHYSICS
    if body.startswith(b"<roblox"):
        return MODEL  # models (animation-only files are also refused here, which is stricter)
    if body.startswith((b"Kaydara FBX Binary", b"; FBX", b"glTF", b"ply\n", b"ply\r\n")):
        return SCENE
    if len(body) >= 84:
        triangles = struct.unpack_from("<I", body, 80)[0]
        if triangles and triangles * 50 + 84 == len(body):
            return SCENE
    head = body[: 64 << 10].decode("utf-8", "replace").lower()
    stripped = head.lstrip()
    if stripped.startswith("solid") and "facet normal" in head:
        return SCENE
    if stripped.startswith("{") and '"asset"' in head and ('"meshes"' in head or '"accessors"' in head):
        return SCENE
    lines = [line.lstrip() for line in head.splitlines()]
    if sum(line.startswith("v ") for line in lines) >= 3 and any(line.startswith("f ") for line in lines):
        return SCENE
    if body.startswith(b"\x28\xb5\x2f\xfd"):
        try:
            import zstandard

            inside = zstandard.ZstdDecompressor().decompressobj().decompress(body)[: 64 << 10]
        except Exception:
            inside = b""
        return verdict(inside) if inside and not inside.startswith(b"\x28\xb5\x2f\xfd") else CLEAR
    return CLEAR


def _derive_key() -> int:
    """The checksum key, folded from the rules' verdicts exactly as rbxcache does."""
    bodies = [MESH, MESH, MESH, PHYSICS, MODEL, MODEL, CLEAR, SCENE, SCENE, SCENE, SCENE, SCENE, MESH, SCENE,
              CLEAR, CLEAR, CLEAR, CLEAR]
    seen = b"".join(struct.pack("<I", _code(v)) for v in bodies)
    for asset_type in range(96):
        seen += struct.pack("<I", _code(type_verdict(asset_type)))
        seen += struct.pack("<I", _code(PHYSICS if asset_type == PHYSICS_CATEGORY else CLEAR))
    seen += struct.pack("<I", _code(MESH)) + struct.pack("<I", _code(CLEAR))
    return xxhash.xxh32_intdigest(seen, seed=0x2545F491) ^ _ANCHOR


CHECKSUM_KEY = _derive_key()


def checksum(data: bytes) -> int:
    """The XXH32 checksum Roblox stores for headers and bodies (0 for empty data)."""
    return xxhash.xxh32_intdigest(data, seed=CHECKSUM_KEY) if data else 0


class Refused(Exception):
    """A write Patchwork won't make (see :func:`check_write`)."""


def check_write(current: bytes, asset_type: int | None, category: int, replacement: bytes) -> None:
    """Raise Refused unless replacing ``current`` with ``replacement`` is allowed.

    * Collision data (category 22) is never written.
    * 2D content (images, sounds, ...) may become any other 2D content.
    * 3D content may only become 3D content of the same kind (mesh -> mesh,
      model -> model): a skin swap. It can't be blanked or turned into something
      else, so nothing 3D can be made to disappear.
    * 2D content can't become 3D content.
    """
    if category == PHYSICS_CATEGORY:
        raise Refused("that entry is collision data, which Patchwork never changes")
    was = verdict(current)
    if was == CLEAR and type_verdict(asset_type) != CLEAR:
        was = MODEL  # a 3D asset type whose body didn't reveal its kind
    new = verdict(replacement)
    if was == CLEAR:
        if new != CLEAR:
            raise Refused("3D content can only replace 3D content")
        return
    if not replacement:
        raise Refused("3D objects can't be removed (that would make them invisible); swap them for another skin "
                      "instead")
    if new != was:
        raise Refused(f"a {was} can only be swapped for another {was}")


# ---------------------------------------------------------------------------------------------
# The RBXH wrapper


@dataclass(frozen=True)
class Header:
    url: str
    url_len: int
    status: int
    headers: str
    header_sum: int
    body_len: int
    body_sum: int
    body_offset: int

    @property
    def asset_type(self) -> int | None:
        for line in self.headers.splitlines():
            name, _, value = line.partition(":")
            if name.strip().lower() == "roblox-assettypeid":
                try:
                    return int(value.strip())
                except ValueError:
                    return None
        return None


def parse_header(buf: bytes) -> Header | None:
    """Parse the RBXH wrapper at the start of ``buf``; None for unwrapped blobs."""
    if len(buf) < 37 or buf[:4] != MAGIC:
        return None
    url_len = struct.unpack_from("<I", buf, 8)[0]
    o = 12 + url_len
    if len(buf) < o + 25:
        return None
    status, header_len, header_sum, body_len, body_sum = struct.unpack_from("<IIIII", buf, o + 1)
    headers_at = o + 25
    body_offset = headers_at + header_len
    if len(buf) < body_offset:
        return None
    return Header(
        url=buf[12:o].decode("utf-8", "replace"), url_len=url_len, status=status,
        headers=buf[headers_at:body_offset].decode("utf-8", "replace"), header_sum=header_sum,
        body_len=body_len, body_sum=body_sum, body_offset=body_offset,
    )


def wrap(url: str, headers: str, body: bytes, status: int = 200, version: int = 1) -> bytes:
    """Build a wrapped blob (used by tests and to describe the format)."""
    u, h = url.encode(), headers.encode()
    return (MAGIC + struct.pack("<II", version, len(u)) + u + b"\x00"
            + struct.pack("<IIIIII", status, len(h), checksum(h), len(body), checksum(body), 0) + h + body)


def with_new_body(old: bytes, header: Header, body: bytes) -> bytes:
    """``old`` with its body replaced, keeping URL, status and headers; length and checksum updated."""
    out = bytearray(old[: header.body_offset])
    at = 12 + header.url_len + 13
    struct.pack_into("<II", out, at, len(body), checksum(body))
    return bytes(out) + body


def sniff(body: bytes) -> str:
    """Rough content type from the first bytes."""
    checks = [
        (b"<roblox!", "rbxm"), (b"<roblox", "rbxmx"), (b"\x89PNG", "png"), (b"\xff\xd8\xff", "jpeg"),
        (b"GIF8", "gif"), (b"\xabKTX 20", "ktx2"), (b"\xabKTX", "ktx"), (b"OggS", "ogg"), (b"ID3", "mp3"),
        (b"fLaC", "flac"), (b"\x28\xb5\x2f\xfd", "zstd"), (b"version ", "mesh"),
    ]
    if not body:
        return "empty"
    for magic, name in checks:
        if body.startswith(magic):
            return name
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "webp"
    if body[:4] == b"RIFF" and body[8:12] == b"WAVE":
        return "wav"
    if body[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "mp3"
    return "other"


# ---------------------------------------------------------------------------------------------
# The store


@dataclass(frozen=True)
class Entry:
    id: bytes
    size: int
    hits: int
    atime: int
    category: int
    inline: bool

    @property
    def hex(self) -> str:
        return self.id.hex()


def default_dir() -> Path | None:
    local = os.environ.get("LOCALAPPDATA")
    return Path(local) / "Roblox" if local else None


class Store:
    """One Roblox cache: the index plus its blob folder."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.db_path = self.directory / "rbx-storage.db"
        self.blobs = self.directory / "rbx-storage"

    def exists(self) -> bool:
        return self.db_path.is_file()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5, isolation_level=None)
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def blob_path(self, entry_id: bytes) -> Path:
        h = entry_id.hex()
        return self.blobs / h[:2] / h

    def data_version(self, conn: sqlite3.Connection) -> int:
        return conn.execute("PRAGMA data_version").fetchone()[0]

    def entries(self, conn: sqlite3.Connection) -> list[Entry]:
        rows = conn.execute("SELECT id, size, hits, atime, category, content IS NOT NULL FROM files").fetchall()
        return [Entry(bytes(r[0]), r[1] or 0, r[2] or 0, r[3] or 0, r[4] or 0, bool(r[5])) for r in rows]

    def read_raw(self, conn: sqlite3.Connection, entry_id: bytes) -> bytes:
        row = conn.execute("SELECT content FROM files WHERE id = ?", (entry_id,)).fetchone()
        if row is None:
            raise KeyError(entry_id.hex())
        if row[0] is not None:
            return bytes(row[0])
        return self.blob_path(entry_id).read_bytes()

    def read_head(self, conn: sqlite3.Connection, entry: Entry, limit: int = 16 << 10) -> bytes:
        """The start of an entry's stored bytes (enough for the wrapper and file magic)."""
        if entry.inline:
            row = conn.execute("SELECT content FROM files WHERE id = ?", (entry.id,)).fetchone()
            return bytes(row[0]) if row and row[0] is not None else b""
        try:
            with open(self.blob_path(entry.id), "rb") as fh:
                return fh.read(limit)
        except OSError:
            return b""

    def read_body(self, conn: sqlite3.Connection, entry_id: bytes) -> tuple[bytes, Header | None]:
        raw = self.read_raw(conn, entry_id)
        header = parse_header(raw)
        return (raw[header.body_offset:] if header else raw), header

    def replace_body(self, conn: sqlite3.Connection, entry_id: bytes, body: bytes) -> None:
        """Swap an entry's payload for ``body``. The index is committed only once the file is in place."""
        old = self.read_raw(conn, entry_id)
        header = parse_header(old)
        current = old[header.body_offset:] if header else old
        category = conn.execute("SELECT category FROM files WHERE id = ?", (entry_id,)).fetchone()[0]
        check_write(current, header.asset_type if header else None, category, body)
        new = with_new_body(old, header, body) if header else body
        path = self.blob_path(entry_id)
        conn.execute("BEGIN IMMEDIATE")
        try:
            if len(new) > INLINE_MAX:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(path.name + ".tmp")
                try:
                    tmp.write_bytes(new)
                    conn.execute("UPDATE files SET content = NULL, size = ? WHERE id = ?", (len(new), entry_id))
                    os.replace(tmp, path)
                finally:
                    tmp.unlink(missing_ok=True)
            else:
                conn.execute("UPDATE files SET content = ?, size = ? WHERE id = ?", (new, len(new), entry_id))
                path.unlink(missing_ok=True)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()
