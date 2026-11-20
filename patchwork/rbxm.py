"""Peek inside Roblox model files (.rbxm / .rbxmx) to say what they are.

Animations are model files too (a KeyframeSequence or CurveAnimation), so in
Roblox's cache they look like any other model. This reads just enough of the
file (the list of instance classes and their names) to label animations, so
they can be found in the Cache tab. Nothing is changed or run.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field

BINARY_MAGIC = b"<roblox!\x89\xff\r\n\x1a\n"
ANIMATION_CLASSES = {"KeyframeSequence", "CurveAnimation"}


class ModelError(ValueError):
    pass


@dataclass
class Summary:
    classes: dict[str, int] = field(default_factory=dict)  # class name -> instance count
    names: list[str] = field(default_factory=list)  # names of the top-level-looking instances

    @property
    def is_animation(self) -> bool:
        return any(c in ANIMATION_CLASSES for c in self.classes)

    @property
    def label(self) -> str:
        if self.is_animation:
            return "Animation"
        if "Sound" in self.classes:
            return "Model with sounds"
        if any(c in self.classes for c in ("ParticleEmitter", "Trail", "Beam")):
            return "Model with effects"
        if "MeshPart" in self.classes:
            return "Model (mesh parts)"
        return "Model"

    def describe(self) -> str:
        top = sorted(self.classes.items(), key=lambda kv: -kv[1])[:5]
        parts = ", ".join(f"{n} {c}" for c, n in top)
        name = f"“{self.names[0]}” · " if self.names else ""
        return f"{name}{parts}" if parts else name.rstrip(" ·")


def _chunk_data(data: bytes, off: int) -> tuple[str, bytes, int]:
    name = data[off:off + 4].rstrip(b"\0").decode("ascii", "replace")
    comp, raw_len, _ = struct.unpack_from("<3I", data, off + 4)
    start = off + 16
    if comp == 0:
        return name, data[start:start + raw_len], start + raw_len
    blob = data[start:start + comp]
    if blob.startswith(b"\x28\xb5\x2f\xfd"):
        import zstandard
        payload = zstandard.ZstdDecompressor().decompress(blob, max_output_size=raw_len)
    else:
        try:
            import lz4.block
        except ImportError as exc:
            raise ModelError("LZ4 support isn't in this build") from exc
        payload = lz4.block.decompress(blob, uncompressed_size=raw_len)
    return name, payload, start + comp


def summarise(data: bytes) -> Summary:
    if data.startswith(BINARY_MAGIC):
        return _binary(data)
    if data.lstrip()[:8] == b"<roblox " or data.lstrip().startswith(b"<roblox xmlns"):
        text = data.decode("utf-8", "replace")
        s = Summary()
        for c in re.findall(r'<Item class="([^"]+)"', text):
            s.classes[c] = s.classes.get(c, 0) + 1
        s.names = re.findall(r'<string name="Name">([^<]{1,60})</string>', text)[:3]
        return s
    raise ModelError("not a Roblox model")


def _binary(data: bytes) -> Summary:
    s = Summary()
    class_names: dict[int, str] = {}
    off = 32
    for _ in range(100000):
        if off + 16 > len(data):
            break
        name, payload, off = _chunk_data(data, off)
        if name == "INST" and len(payload) >= 13:
            cid, nlen = struct.unpack_from("<2I", payload, 0)
            cname = payload[8:8 + nlen].decode("utf-8", "replace")
            count = struct.unpack_from("<I", payload, 8 + nlen + 1)[0]
            class_names[cid] = cname
            s.classes[cname] = s.classes.get(cname, 0) + count
        elif name == "PROP" and len(payload) >= 9 and not s.names:
            cid, plen = struct.unpack_from("<2I", payload, 0)
            pname = payload[8:8 + plen]
            pos = 8 + plen
            if pname == b"Name" and pos < len(payload) and payload[pos] == 0x01 and \
                    class_names.get(cid) in ANIMATION_CLASSES | {"Model", "Tool", "Folder", "MeshPart", "Sound"}:
                pos += 1
                if pos + 4 <= len(payload):
                    slen = struct.unpack_from("<I", payload, pos)[0]
                    if 0 < slen <= 80:
                        s.names.append(payload[pos + 4:pos + 4 + slen].decode("utf-8", "replace"))
        elif name == "END":
            break
    return s
