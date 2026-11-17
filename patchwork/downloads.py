"""Download and cache the files that web-link swaps point at.

Patchwork fetches each link itself and serves the copy to Roblox, so Roblox
only ever talks to its own asset server. Files are cached by URL in
``<app dir>/downloads`` and reused until the cache is cleared.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import urllib.request
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from . import __version__

MAX_BYTES = 50 * 1024 * 1024
TIMEOUT = 30
KNOWN_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".tga", ".ogg", ".mp3", ".wav", ".mesh", ".rbxm", ".rbxmx"}


class DownloadError(Exception):
    pass


def cache_name(url: str) -> str:
    ext = PurePosixPath(urlparse(url).path).suffix.lower()
    if ext not in KNOWN_EXTS:
        ext = ".bin"
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:32] + ext


def fetch(url: str, dest: Path) -> Path:
    """Download ``url`` to ``dest`` (blocking). Raises DownloadError with a readable reason."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise DownloadError(f"not a web link: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": f"Patchwork/{__version__}"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as resp:
            data = resp.read(MAX_BYTES + 1)
    except OSError as exc:  # URLError, HTTPError and timeouts are all OSErrors
        raise DownloadError(f"{parsed.netloc}: {getattr(exc, 'reason', exc)}") from exc
    if len(data) > MAX_BYTES:
        raise DownloadError(f"{url} is larger than {MAX_BYTES // (1024 * 1024)} MB")
    if not data:
        raise DownloadError(f"{url} returned an empty file")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, dest)
    return dest


class Downloads:
    """Async-friendly cache: concurrent requests for the same link share one download."""

    def __init__(self, directory: Path):
        self.directory = directory
        self._locks: dict[str, asyncio.Lock] = {}

    def path_for(self, url: str) -> Path:
        return self.directory / cache_name(url)

    def cached(self, url: str) -> Path | None:
        path = self.path_for(url)
        return path if path.is_file() else None

    async def get(self, url: str) -> Path:
        if (hit := self.cached(url)) is not None:
            return hit
        lock = self._locks.setdefault(url, asyncio.Lock())
        async with lock:
            if (hit := self.cached(url)) is not None:
                return hit
            return await asyncio.to_thread(fetch, url, self.path_for(url))
