"""Skin capture: learn which cache files a skin uses by watching Roblox's cache.

RIVALS' own skins, animations and sounds are only handed out to signed-in players,
so Patchwork can't look them up by asset ID (it never uses your Roblox login).
Instead you show a skin in game while Patchwork notes which cache entries Roblox
writes or reads. Doing the same for the skin you want gives the files to copy.

Copies are like-for-like only: a mesh replaces a mesh, a KTX2 texture a KTX2
texture, a model a model, a sound a sound. Every write still goes through
``cache.check_write``, so nothing can be removed or made invisible.
"""

from __future__ import annotations

import base64
import json
import re
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import cache

GROUPS = {
    "mesh": "Mesh", "rbxm": "Model / animation", "ktx2": "Texture (KTX2)", "ktx": "Texture (KTX)",
    "png": "Image (PNG)", "webp": "Image (WEBP)", "jpg": "Image (JPG)", "gif": "Image (GIF)",
    "ogg": "Sound (OGG)", "mp3": "Sound (MP3)", "wav": "Sound (WAV)",
}
PREVIEWABLE = {"png": "image/png", "webp": "image/webp", "jpg": "image/jpeg", "gif": "image/gif"}
PREVIEW_MAX = 400 << 10
_NAME = re.compile(r"[^\w .()\-]+")


@dataclass
class Seen:
    """One cache entry that changed while capturing."""

    hex: str
    kind: str
    size: int
    label: str
    preview: str = ""  # data: URL for images


@dataclass
class Pair:
    target: str  # cache entry (hex) that holds the skin being replaced
    source: str  # md5 of the replacement body, saved under captured/<md5>.body
    kind: str


@dataclass
class Capture:
    name: str
    pairs: list[Pair] = field(default_factory=list)
    enabled: bool = True

    @property
    def key(self) -> str:
        return f"capture:{self.name}"


def _label(url: str, kind: str) -> str:
    host = re.sub(r"^https?://([^/]+).*$", r"\1", url) if url else ""
    tail = url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1][:12] if url else ""
    return f"{GROUPS.get(kind, kind)}" + (f" · {host} …{tail}" if host else "")


class Captures:
    """The saved captured swaps (captures.json) and their replacement files."""

    def __init__(self, home: Path):
        self.path = home / "captures.json"
        self.bodies = home / "captured"
        self._lock = threading.RLock()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.items = [Capture(c["name"], [Pair(**p) for p in c.get("pairs", [])], c.get("enabled", True))
                          for c in raw]
        except (OSError, ValueError, KeyError, TypeError):
            self.items = []

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps([asdict(c) for c in self.items], indent=1), encoding="utf-8")
            tmp.replace(self.path)

    def body(self, md5: str) -> bytes | None:
        try:
            return (self.bodies / f"{md5}.body").read_bytes()
        except OSError:
            return None

    def keep_body(self, body: bytes) -> str:
        digest = cache.md5(body)
        self.bodies.mkdir(parents=True, exist_ok=True)
        path = self.bodies / f"{digest}.body"
        if not path.exists():
            path.write_bytes(body)
        return digest

    def get(self, name: str) -> Capture | None:
        return next((c for c in self.items if c.name == name), None)

    def add(self, capture: Capture) -> None:
        with self._lock:
            capture.name = clean_name(capture.name)
            self.items = [c for c in self.items if c.name != capture.name] + [capture]
            self.save()

    def remove(self, name: str) -> None:
        with self._lock:
            self.items = [c for c in self.items if c.name != name]
            used = {p.source for c in self.items for p in c.pairs}
            self.save()
            if self.bodies.is_dir():
                for f in self.bodies.glob("*.body"):
                    if f.stem not in used:
                        f.unlink(missing_ok=True)

    def set_enabled(self, name: str, enabled: bool) -> None:
        with self._lock:
            c = self.get(name)
            if c:
                c.enabled = bool(enabled)
                self.save()

    def active(self) -> list[Capture]:
        return [c for c in self.items if c.enabled]


def clean_name(name: str) -> str:
    name = _NAME.sub("", str(name)).strip()[:60]
    return name or "Captured skin"


def preview(kind: str, body: bytes) -> str:
    mime = PREVIEWABLE.get(kind)
    if not mime or len(body) > PREVIEW_MAX:
        return ""
    return f"data:{mime};base64,{base64.b64encode(body).decode()}"


def auto_pairs(original: list[Seen], replacement: list[Seen]) -> dict[str, str]:
    """A first guess: the nth entry of each kind (largest first) pairs with the nth of the same kind."""
    out: dict[str, str] = {}
    by_kind: dict[str, list[Seen]] = {}
    for s in sorted(replacement, key=lambda s: -s.size):
        by_kind.setdefault(s.kind, []).append(s)
    used: dict[str, int] = {}
    for s in sorted(original, key=lambda s: -s.size):
        options = by_kind.get(s.kind, [])
        i = used.get(s.kind, 0)
        if i < len(options):
            out[s.hex] = options[i].hex
            used[s.kind] = i + 1
    return out
