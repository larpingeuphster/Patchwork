"""Read and write KTX2 textures, the format Roblox stores most in-game textures in.

Patchwork uses this to (1) recognise a cached texture by what it looks like, since
Roblox only hands the KTX2 version to signed-in players and its bytes never match
the PNG anyone can download, and (2) write a replacement texture in the same
format, size and number of mip levels as the one it replaces.

Block-compressed formats are decoded and encoded through Pillow's DDS support
(BC1/BC3 encode; BC1-BC7 decode). Uncompressed RGBA8 is handled directly.
Zstandard supercompression is supported; Basis Universal isn't.
"""

from __future__ import annotations

import io
import struct
from dataclasses import dataclass

MAGIC = b"\xabKTX 20\xbb\r\n\x1a\n"

# vkFormat -> (name, block bytes (0 = uncompressed), DDS FourCC or DXGI format, srgb)
FORMATS = {
    37: ("RGBA8", 0, None, False), 43: ("RGBA8", 0, None, True),
    131: ("BC1", 8, b"DXT1", False), 132: ("BC1", 8, b"DXT1", True),
    133: ("BC1", 8, b"DXT1", False), 134: ("BC1", 8, b"DXT1", True),
    135: ("BC2", 16, b"DXT3", False), 136: ("BC2", 16, b"DXT3", True),
    137: ("BC3", 16, b"DXT5", False), 138: ("BC3", 16, b"DXT5", True),
    139: ("BC4", 8, b"ATI1", False), 141: ("BC5", 16, b"ATI2", False),
    145: ("BC7", 16, 98, False), 146: ("BC7", 16, 99, True),
}
WRITABLE = {"RGBA8", "BC1", "BC3"}
SUPERCOMPRESSION = {0: "none", 1: "BasisLZ", 2: "zstd", 3: "zlib"}


class KtxError(ValueError):
    pass


@dataclass
class Level:
    offset: int
    length: int
    raw_length: int


@dataclass
class Texture:
    vk_format: int
    width: int
    height: int
    levels: list[Level]
    supercompression: int
    dfd: bytes
    kvd: bytes
    data: bytes

    @property
    def format_name(self) -> str:
        return FORMATS.get(self.vk_format, (f"vkFormat {self.vk_format}",))[0]

    def describe(self) -> str:
        return (f"{self.format_name} {self.width}x{self.height}, {len(self.levels)} level(s), "
                f"{SUPERCOMPRESSION.get(self.supercompression, self.supercompression)}")

    @property
    def readable(self) -> bool:
        return self.vk_format in FORMATS and self.supercompression in (0, 2, 3)

    def level_bytes(self, i: int) -> bytes:
        lv = self.levels[i]
        raw = self.data[lv.offset:lv.offset + lv.length]
        if self.supercompression == 2:
            import zstandard
            return zstandard.ZstdDecompressor().decompress(raw, max_output_size=max(lv.raw_length, 1))
        if self.supercompression == 3:
            import zlib
            return zlib.decompress(raw)
        if self.supercompression != 0:
            raise KtxError(f"{SUPERCOMPRESSION.get(self.supercompression)} textures aren't supported")
        return raw


def parse(data: bytes) -> Texture:
    if not data.startswith(MAGIC) or len(data) < 80:
        raise KtxError("not a KTX2 file")
    (vk, _type_size, w, h, _d, _layers, faces, level_count, scheme) = struct.unpack_from("<9I", data, 12)
    dfd_off, dfd_len, kvd_off, kvd_len = struct.unpack_from("<4I", data, 48)
    level_count = max(level_count, 1)
    if faces > 1:
        raise KtxError("cube maps aren't supported")
    levels = []
    for i in range(level_count):
        off, length, raw = struct.unpack_from("<3Q", data, 80 + 24 * i)
        if off + length > len(data):
            raise KtxError("truncated KTX2 file")
        levels.append(Level(off, length, raw))
    return Texture(vk, w, h, levels, scheme, data[dfd_off:dfd_off + dfd_len], data[kvd_off:kvd_off + kvd_len], data)


def _level_size(w: int, h: int, i: int) -> tuple[int, int]:
    return max(1, w >> i), max(1, h >> i)


def _dds(fourcc_or_dxgi, w: int, h: int, payload: bytes) -> bytes:
    """A minimal DDS file around one block-compressed level, for Pillow to decode."""
    flags = 0x1 | 0x2 | 0x4 | 0x1000 | 0x80000
    if isinstance(fourcc_or_dxgi, bytes):
        pf = struct.pack("<II4s5I", 32, 0x4, fourcc_or_dxgi, 0, 0, 0, 0, 0)
        extra = b""
    else:
        pf = struct.pack("<II4s5I", 32, 0x4, b"DX10", 0, 0, 0, 0, 0)
        extra = struct.pack("<5I", fourcc_or_dxgi, 3, 0, 1, 0)
    header = struct.pack("<7I44x", 124, flags, h, w, len(payload), 0, 1) + pf + struct.pack("<5I", 0x1000, 0, 0, 0, 0)
    return b"DDS " + header + extra + payload


def decode(tex: Texture, level: int = 0):
    """One mip level as a Pillow RGBA image."""
    from PIL import Image
    if not tex.readable:
        raise KtxError(f"{tex.describe()} can't be read")
    name, block, code, _srgb = FORMATS[tex.vk_format]
    w, h = _level_size(tex.width, tex.height, level)
    payload = tex.level_bytes(level)
    if block == 0:
        return Image.frombytes("RGBA", (w, h), payload[:w * h * 4])
    img = Image.open(io.BytesIO(_dds(code, w, h, payload)))
    img.load()
    return img.convert("RGBA")


def preview_level(tex: Texture, at_least: int = 32) -> int:
    """The smallest mip level that's still at least ``at_least`` pixels on its long side."""
    best = 0
    for i in range(len(tex.levels)):
        w, h = _level_size(tex.width, tex.height, i)
        if max(w, h) >= at_least:
            best = i
    return best


def _encode_level(img, name: str) -> bytes:
    if name == "RGBA8":
        return img.convert("RGBA").tobytes()
    out = io.BytesIO()
    img.convert("RGBA").save(out, "DDS", pixel_format="DXT1" if name == "BC1" else "DXT5")
    raw = out.getvalue()
    return raw[128:]  # FourCC header, no DX10 extension


# The Khronos data format descriptors for the formats Patchwork writes (KTX2 needs one).
def _dfd(name: str, srgb: bool) -> bytes:
    transfer = 2 if srgb else 1
    if name == "RGBA8":
        model, dims, plane, samples = 1, (0, 0, 0, 0), 4, [(i * 8, 7, ch, 0, 255) for i, ch in enumerate((0, 1, 2, 15))]
    elif name == "BC1":
        model, dims, plane, samples = 128, (3, 3, 0, 0), 8, [(0, 63, 0, 0, 0xFFFFFFFF)]
    else:  # BC3
        model, dims, plane, samples = 130, (3, 3, 0, 0), 16, [(0, 63, 15, 0, 0xFFFFFFFF), (64, 63, 0, 0, 0xFFFFFFFF)]
    block_size = 24 + 16 * len(samples)
    body = struct.pack("<IHHBBBB4B8B", 0, 2, block_size, model, 1, transfer, 0, *dims, plane, 0, 0, 0, 0, 0, 0, 0)
    for bit_offset, bit_len, channel, _lo, hi in samples:
        body += struct.pack("<HBB4BII", bit_offset, bit_len, channel, 0, 0, 0, 0, 0, hi)
    return struct.pack("<I", 4 + len(body)) + body


def encode_like(img, like: Texture) -> bytes:
    """``img`` as a KTX2 file with ``like``'s size and mip count, in its format when Patchwork can
    write that (BC1, BC3, RGBA8), otherwise BC3. Supercompression is kept when it's zstd or none."""
    from PIL import Image
    name, _b, _c, srgb = FORMATS.get(like.vk_format, ("BC3", 16, b"DXT5", True))
    vk = like.vk_format
    dfd = like.dfd
    if name not in WRITABLE:
        name, vk, dfd = "BC3", 138 if srgb else 137, _dfd("BC3", srgb)
    base = img.convert("RGBA").resize((like.width, like.height), Image.LANCZOS)
    scheme = like.supercompression if like.supercompression in (0, 2) else 0
    levels = []
    for i in range(len(like.levels)):
        raw = _encode_level(base.resize(_level_size(like.width, like.height, i), Image.LANCZOS)
                            if i else base, name)
        if scheme == 2:
            import zstandard
            stored = zstandard.ZstdCompressor(level=10).compress(raw)
        else:
            stored = raw
        levels.append((stored, len(raw)))
    return _build(vk, like.width, like.height, levels, scheme, dfd or _dfd(name, srgb), b"")


def _build(vk: int, w: int, h: int, levels: list[tuple[bytes, int]], scheme: int, dfd: bytes, kvd: bytes) -> bytes:
    header_len = 80 + 24 * len(levels)
    dfd_off = header_len
    kvd_off = dfd_off + len(dfd) if kvd else 0
    pos = dfd_off + len(dfd) + len(kvd)
    # Levels are stored smallest first, each aligned to 16 bytes (enough for every block format).
    placed = {}
    chunks = []
    for i in reversed(range(len(levels))):
        pad = (-pos) % 16
        chunks.append(b"\0" * pad)
        pos += pad
        placed[i] = pos
        chunks.append(levels[i][0])
        pos += len(levels[i][0])
    type_size = 1
    head = MAGIC + struct.pack("<9I", vk, type_size, w, h, 0, 0, 1, len(levels), scheme)
    head += struct.pack("<4I2Q", dfd_off, len(dfd), kvd_off, len(kvd), 0, 0)
    for i, (stored, raw_len) in enumerate(levels):
        head += struct.pack("<3Q", placed[i], len(stored), raw_len)
    return head + dfd + kvd + b"".join(chunks)


def make(img, name: str = "BC3", levels: int = 1, srgb: bool = True, zstd: bool = False) -> bytes:
    """A new KTX2 file (used by tests and as a template)."""
    from PIL import Image
    vk = {"RGBA8": 43 if srgb else 37, "BC1": 132 if srgb else 131, "BC3": 138 if srgb else 137}[name]
    base = img.convert("RGBA")
    out = []
    for i in range(levels):
        raw = _encode_level(base.resize(_level_size(base.width, base.height, i), Image.LANCZOS) if i else base, name)
        if zstd:
            import zstandard
            out.append((zstandard.ZstdCompressor().compress(raw), len(raw)))
        else:
            out.append((raw, len(raw)))
    return _build(vk, base.width, base.height, out, 2 if zstd else 0, _dfd(name, srgb), b"")


# ---------------------------------------------------------------------------------------------
# Recognising a texture by its look


SIG = 16


def signature(img) -> bytes:
    """A tiny colour thumbnail (alpha pre-multiplied onto grey) used to compare two images."""
    from PIL import Image
    rgba = img.convert("RGBA").resize((SIG, SIG), Image.BILINEAR)
    grey = Image.new("RGBA", rgba.size, (128, 128, 128, 255))
    return Image.alpha_composite(grey, rgba).convert("RGB").tobytes()


def distance(a: bytes, b: bytes) -> float:
    """Mean absolute difference per channel, 0-255."""
    if len(a) != len(b) or not a:
        return 255.0
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def same_shape(w1: int, h1: int, w2: int, h2: int) -> bool:
    return abs(w1 / max(h1, 1) - w2 / max(h2, 1)) < 0.02
