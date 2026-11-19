"""Apply swaps by rewriting entries in Roblox's asset cache.

Roblox's cache doesn't record asset IDs, so each target asset is downloaded once
from Roblox (as a signed-out visitor) and fingerprinted (MD5 and size); a cache
entry whose body has the same fingerprint is that asset. When Roblox downloads
an asset Patchwork has a swap for, the entry is backed up and its body replaced.
Turning a swap off, or quitting, puts the originals back.

Signed-out downloads work for most images, meshes and models. Roblox only
gives most sounds to signed-in players, and Patchwork never uses your account,
so those can't be matched automatically (Activity says which).

Skin swaps (mesh -> mesh, model -> model) are allowed; making 3D objects
disappear is not (see cache.check_write).
"""

from __future__ import annotations

import io
import json
import re
import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import __version__, cache
from .config import KIND_ASSET, KIND_FILE, KIND_REMOVE, KIND_URL, Swap
from .downloads import DownloadError, Downloads
from .rules import Hit

log = logging.getLogger("patchwork")

ASSET_URL = "https://assetdelivery.roblox.com/v1/asset/?id={id}"
MAX_ASSET = 50 * 1024 * 1024
RETRY_REFUSED_AFTER = 6 * 60 * 60  # Roblox said no (sign-in needed, missing asset)
RETRY_NETWORK_AFTER = 2 * 60  # couldn't reach Roblox: try again soon
IMAGE_KINDS = {"png", "jpeg", "gif", "webp"}
_ID_IN_URL = re.compile(r"(?:[?&]id=|/assetid/|/asset-id/)(\d+)", re.IGNORECASE)


@dataclass(frozen=True)
class EntryInfo:
    body_len: int
    kind: str
    url: str = ""
    status: int = 0
    location: str = ""  # where a cached redirect points


def asset_id_in(url: str) -> int | None:
    """The asset ID an asset-delivery URL asks for, if it names one."""
    if "assetdelivery" not in url.lower() and "/asset" not in url.lower():
        return None
    match = _ID_IN_URL.search(url)
    return int(match.group(1)) if match else None


def _location(headers: str) -> str:
    for line in headers.splitlines():
        name, _, value = line.partition(":")
        if name.strip().lower() == "location":
            return value.strip()
    return ""


class Unavailable(Exception):
    """An asset Patchwork can't download or use, with a readable reason."""

    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.transient = transient  # a network problem, worth retrying soon


def fetch_asset(asset_id: int, timeout: float = 15) -> bytes:
    """Download an asset's file from Roblox as a signed-out visitor."""
    request = urllib.request.Request(ASSET_URL.format(id=asset_id), headers={"User-Agent": f"Patchwork/{__version__}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            asset_type = resp.headers.get("roblox-assettypeid")
            body = resp.read(MAX_ASSET + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise Unavailable("Roblox only hands this asset out to signed-in players in the game (RIVALS' own "
                              "sounds, meshes and animations are like this)") from exc
        if exc.code == 404:
            raise Unavailable("Roblox says this asset doesn't exist") from exc
        raise Unavailable(f"Roblox answered {exc.code}", transient=exc.code >= 500 or exc.code == 429) from exc
    except OSError as exc:
        raise Unavailable(f"couldn't reach Roblox: {getattr(exc, 'reason', exc)}", transient=True) from exc
    if len(body) > MAX_ASSET:
        raise Unavailable("the asset is larger than 50 MB")
    return body


@dataclass
class Fingerprint:
    md5: str
    size: int
    kind: str


class IdMap:
    """Remembered fingerprints of asset IDs (and failures, so Roblox isn't asked every few seconds)."""

    def __init__(self, path: Path, fetch: Callable[[int], bytes] = fetch_asset):
        self.path = path
        self.fetch = fetch
        self._data: dict[str, dict] = {}
        try:
            self._data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

    def get(self, asset_id: int) -> Fingerprint | str:
        """The asset's fingerprint, or a reason it isn't available."""
        rec = self._data.get(str(asset_id))
        now = time.time()
        if rec and "md5" in rec:
            return Fingerprint(rec["md5"], rec["size"], rec.get("kind", "other"))
        if rec and now - rec.get("at", 0) < (RETRY_NETWORK_AFTER if rec.get("transient") else RETRY_REFUSED_AFTER):
            return rec.get("error", "unavailable")
        try:
            body = self.fetch(asset_id)
        except Unavailable as exc:
            self._data[str(asset_id)] = {"error": str(exc), "at": now, "transient": exc.transient}
            self.save()
            return str(exc)
        fp = Fingerprint(cache.md5(body), len(body), cache.sniff(body))
        self._data[str(asset_id)] = {"md5": fp.md5, "size": fp.size, "kind": fp.kind, "at": now}
        self.save()
        return fp


def fit_image(data: bytes, like: bytes) -> bytes:
    """``data`` as an image in a format the cached original's slot can take (PNG unless it's the same format)."""
    if cache.sniff(data) == cache.sniff(like):
        return data
    try:
        from PIL import Image
    except ImportError as exc:
        raise Unavailable("image conversion isn't available in this build") from exc
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:
        raise Unavailable("the replacement isn't an image Patchwork can read") from exc
    out = io.BytesIO()
    img.convert("RGBA").save(out, format="PNG")
    return out.getvalue()


def blank_like(original: bytes) -> bytes:
    """What a Remove swap writes: a transparent image the original's size, or nothing for other kinds."""
    if cache.sniff(original) in IMAGE_KINDS:
        try:
            from PIL import Image

            size = Image.open(io.BytesIO(original)).size
            out = io.BytesIO()
            Image.new("RGBA", size, (0, 0, 0, 0)).save(out, format="PNG")
            return out.getvalue()
        except Exception:
            pass
    return b""


class Backups:
    """Originals of entries Patchwork has rewritten, so they can be put back."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.manifest_path = directory / "manifest.json"
        try:
            self.manifest: dict[str, dict] = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.manifest = {}

    def save(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self.manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.manifest, indent=1), encoding="utf-8")
        tmp.replace(self.manifest_path)

    def keep(self, entry_hex: str, original: bytes, asset_id: int, written: bytes, swap_key: str) -> None:
        path = self.directory / f"{entry_hex}.body"
        if entry_hex not in self.manifest or not path.exists():
            self.directory.mkdir(parents=True, exist_ok=True)
            path.write_bytes(original)
            self.manifest[entry_hex] = {"asset_id": asset_id, "original_md5": cache.md5(original)}
        self.manifest[entry_hex].update(written_md5=cache.md5(written), swap=swap_key)
        self.save()

    def original(self, entry_hex: str) -> bytes | None:
        try:
            return (self.directory / f"{entry_hex}.body").read_bytes()
        except OSError:
            return None

    def forget(self, entry_hex: str) -> None:
        self.manifest.pop(entry_hex, None)
        (self.directory / f"{entry_hex}.body").unlink(missing_ok=True)
        self.save()


def swap_key(swap: Swap) -> str:
    return f"{swap.kind}:{swap.value}"


class CacheSwapper:
    """Keeps Roblox's cache in line with the enabled swaps. ``tick`` is called every few seconds."""

    def __init__(self, store: cache.Store, app_dir: Path, downloads: Downloads,
                 on_hit: Callable[[Hit], None], idmap: IdMap | None = None):
        self.store = store
        self.downloads = downloads
        self.on_hit = on_hit
        self.idmap = idmap or IdMap(app_dir / "idmap.json")
        self.backups = Backups(app_dir / "originals")
        self.swaps: list[Swap] = []
        self._lock = threading.RLock()
        self._last_version: int | None = None
        self._dirty = True
        # Keyed by (id, size, atime) so an entry Roblox rewrites is read again.
        self._body_info: dict[tuple[bytes, int, int], tuple[int, str]] = {}
        self._md5: dict[tuple[bytes, int, int], str] = {}
        self._conn = None  # kept open: SQLite's data_version only compares within one connection
        self._replacements: dict[tuple[str, str], bytes] = {}
        self._reported: set[str] = set()
        self._last_unavailable: tuple = ()
        self._described = False
        self.stats = {"swapped": 0, "waiting": 0, "unavailable": 0}
        self.state: dict[int, str] = {}  # asset id -> "swapped" / "waiting" / reason

    # --- configuration ---------------------------------------------------------------------

    def set_swaps(self, swaps: list[Swap]) -> None:
        with self._lock:
            self.swaps = [s for s in swaps if s.enabled]
            self._replacements = {k: v for k, v in self._replacements.items()
                                  if any(swap_key(s) == k[0] for s in self.swaps)}
            self._dirty = True

    # --- helpers ---------------------------------------------------------------------------

    def _note(self, key: str, message: str, level: int = logging.WARNING) -> None:
        if key not in self._reported:
            self._reported.add(key)
            log.log(level, message)

    def _replacement(self, swap: Swap, original: bytes) -> bytes:
        kind = cache.sniff(original)
        key = (swap_key(swap), cache.md5(original))
        if key in self._replacements:
            return self._replacements[key]
        if swap.kind == KIND_REMOVE:
            if cache.verdict(original) != cache.CLEAR:
                raise Unavailable("3D objects can't be removed (that would make them invisible); swap them for "
                                  "another skin's asset ID instead")
            data = blank_like(original)
        else:
            if swap.kind == KIND_ASSET:
                data = fetch_asset(int(swap.value))
            elif swap.kind == KIND_URL:
                path = self.downloads.cached(swap.value)
                if path is None:
                    from . import downloads as dl
                    try:
                        path = dl.fetch(swap.value, self.downloads.path_for(swap.value))
                    except DownloadError as exc:
                        raise Unavailable(f"couldn't download the replacement: {exc}") from exc
                data = path.read_bytes()
            elif swap.kind == KIND_FILE:
                try:
                    data = Path(swap.value).read_bytes()
                except OSError as exc:
                    raise Unavailable(f"couldn't read {swap.value}") from exc
            else:
                raise Unavailable(f"unknown swap type {swap.kind}")
            if cache.verdict(original) != cache.CLEAR and swap.kind != KIND_ASSET:
                raise Unavailable("3D objects can only be swapped for another Roblox asset ID (another skin)")
            if kind == "ktx2":
                raise Unavailable("Roblox stores this texture as a GPU texture pack, which Patchwork can't "
                                  "write yet")
            if kind in IMAGE_KINDS:
                data = fit_image(data, original)
        self._replacements[key] = data
        return data

    def _wanted(self, linked: set[int]) -> dict[tuple[int, str], tuple[int, Swap]]:
        """(size, md5) of each target's original -> (asset id, swap). Earlier swaps win.

        Targets in ``linked`` were found through the cache's own records and need no download.
        """
        wanted: dict[tuple[int, str], tuple[int, Swap]] = {}
        unavailable: list[tuple[int, str, str]] = []
        for swap in self.swaps:
            for asset_id in swap.targets:
                if asset_id in linked:
                    continue
                fp = self.idmap.get(asset_id)
                if isinstance(fp, str):
                    unavailable.append((asset_id, swap.name, fp))
                    self.state[asset_id] = fp
                    continue
                wanted.setdefault((fp.size, fp.md5), (asset_id, swap))
                if self.state.get(asset_id) != "swapped":
                    self.state[asset_id] = "waiting"
        self.stats.update(unavailable=len(unavailable))
        self._report_unavailable(unavailable)
        return wanted

    def _report_unavailable(self, unavailable: list[tuple[int, str, str]]) -> None:
        """One message for every asset that can't be identified, grouped by reason, only when it changes."""
        key = tuple(sorted((a, r) for a, _, r in unavailable))
        if key == self._last_unavailable:
            return
        self._last_unavailable = key
        by_reason: dict[str, list[str]] = {}
        for asset_id, name, reason in unavailable:
            by_reason.setdefault(reason, []).append(f"{name} ({asset_id})")
        for reason, names in by_reason.items():
            shown = ", ".join(names[:8]) + (f" and {len(names) - 8} more" if len(names) > 8 else "")
            log.warning("%d asset(s) can't be identified in Roblox's cache yet: %s. Affected: %s.",
                        len(names), reason, shown)

    def _info(self, conn, entry: cache.Entry) -> EntryInfo:
        # These don't change while the stored size stays the same, so an entry isn't re-read
        # every time Roblox touches it (which updates its access time).
        key = (entry.id, entry.size, 0)
        if key not in self._body_info:
            head = self.store.read_head(conn, entry)
            header = cache.parse_header(head)
            if header:
                info = EntryInfo(header.body_len, cache.sniff(head[header.body_offset:]), header.url,
                                 header.status, _location(header.headers))
            else:
                info = EntryInfo(entry.size, cache.sniff(head))
            self._body_info[key] = info
        return self._body_info[key]

    def _forget(self, entry_id: bytes) -> None:
        """Drop what's remembered about an entry after Patchwork writes to it."""
        for memo in (self._md5, self._body_info):
            for key in [k for k in memo if k[0] == entry_id]:
                del memo[key]

    def _entry_md5(self, conn, entry: cache.Entry) -> str | None:
        key = (entry.id, entry.size, entry.atime)
        if key not in self._md5:
            try:
                body, _ = self.store.read_body(conn, entry.id)
            except (OSError, KeyError):
                return None
            self._md5[key] = cache.md5(body)
        return self._md5[key]

    # --- the work ------------------------------------------------------------------------------

    def tick(self, force: bool = False) -> None:
        with self._lock:
            if not self.store.exists():
                self.close()
                return
            try:
                if self._conn is None:
                    self._conn = self.store.connect()
                    self._last_version = None
                conn = self._conn
                version = self.store.data_version(conn)
                if not (force or self._dirty or version != self._last_version):
                    return
                self._dirty = False
                self._apply(conn)
                self._last_version = self.store.data_version(conn)
            except Exception as exc:
                self._note(f"tick:{type(exc).__name__}", f"Couldn't update Roblox's cache: {exc}")
                self.close()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    log.exception("Couldn't close Roblox's cache index")
                self._conn = None

    def _links(self, conn, entries: list[cache.Entry]) -> dict[int, cache.Entry]:
        """Asset ID -> cache entry, from the cache's own records (no download needed).

        Roblox may cache its asset-delivery answers, whose URL names the asset ID: either
        the asset itself, or a redirect to the CDN address of the entry that holds it.
        """
        by_url: dict[str, cache.Entry] = {}
        infos = {}
        for entry in entries:
            info = self._info(conn, entry)
            infos[entry.hex] = info
            if info.url:
                by_url.setdefault(info.url, entry)
        links: dict[int, cache.Entry] = {}
        for entry in entries:
            info = infos[entry.hex]
            asset_id = asset_id_in(info.url) if info.url else None
            if asset_id is None:
                continue
            if info.location:
                target = by_url.get(info.location)
                if target is not None:
                    links.setdefault(asset_id, target)
            elif 200 <= info.status < 300 and info.kind not in ("other", "empty"):
                links.setdefault(asset_id, entry)
        if not self._described:
            self._described = True
            self._describe(entries, infos, links)
        return links

    def _describe(self, entries, infos, links) -> None:
        kinds: dict[str, int] = {}
        for entry in entries:
            kind = infos[entry.hex].kind
            kinds[kind] = kinds.get(kind, 0) + 1
        named = sum(1 for e in entries if infos[e.hex].url and asset_id_in(infos[e.hex].url) is not None)
        summary = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items(), key=lambda kv: -kv[1])[:8])
        log.info("Roblox's cache has %d entries (%s). %d name an asset ID, linking %d asset(s) to their files.",
                 len(entries), summary or "none", named, len(links))

    def _apply(self, conn) -> None:
        entries = self.store.entries(conn)
        links = self._links(conn, entries)
        linked: dict[str, tuple[int, Swap]] = {}
        for swap in self.swaps:
            for asset_id in swap.targets:
                entry = links.get(asset_id)
                if entry is not None:
                    linked.setdefault(entry.hex, (asset_id, swap))
        wanted = self._wanted({aid for aid, _ in linked.values()})
        sizes = {size for size, _ in wanted}
        present = {e.hex: e for e in entries}
        active_keys = {swap_key(s) for s in self.swaps}

        # Put back entries whose swap is gone (or changed), if Roblox hasn't replaced them since.
        for entry_hex, rec in list(self.backups.manifest.items()):
            entry = present.get(entry_hex)
            target_still_wanted = any(rec.get("asset_id") == aid and swap_key(s) == rec.get("swap")
                                      for aid, s in list(wanted.values()) + list(linked.values()))
            if target_still_wanted and rec.get("swap") in active_keys:
                continue
            if entry is not None and self._entry_md5(conn, entry) == rec.get("written_md5"):
                original = self.backups.original(entry_hex)
                if original is not None:
                    try:
                        self.store.replace_body(conn, entry.id, original)
                        self._forget(entry.id)
                    except Exception as exc:
                        self._note(f"restore:{entry_hex}", f"Couldn't put back an original in Roblox's cache: {exc}")
                        continue
            self.backups.forget(entry_hex)

        # Swap entries that hold a wanted original.
        swapped = 0
        for entry in entries:
            body_len = self._info(conn, entry).body_len
            rec = self.backups.manifest.get(entry.hex)
            if body_len not in sizes and rec is None and entry.hex not in linked:
                continue
            digest = self._entry_md5(conn, entry)
            if digest is None:
                continue
            if rec and digest == rec.get("written_md5"):
                swapped += 1  # still holds our replacement
                self.state[rec.get("asset_id")] = "swapped"
                continue
            match = linked.get(entry.hex) or wanted.get((body_len, digest))
            if match is None:
                continue
            asset_id, swap = match
            try:
                original, _ = self.store.read_body(conn, entry.id)
                replacement = self._replacement(swap, original)
                self.store.replace_body(conn, entry.id, replacement)
                self._forget(entry.id)
            except (Unavailable, cache.Refused) as exc:
                self.state[asset_id] = str(exc)
                self._note(f"fail:{asset_id}:{swap_key(swap)}", f"Can't swap {asset_id} ({swap.name}): {exc}.")
                continue
            except Exception as exc:
                self._note(f"err:{entry.hex}", f"Couldn't write {asset_id} ({swap.name}) into Roblox's cache: {exc}")
                continue
            self.backups.keep(entry.hex, original, asset_id, replacement, swap_key(swap))
            self.state[asset_id] = "swapped"
            swapped += 1
            try:
                self.on_hit(Hit(asset_id, swap.name, swap.describe()))
            except Exception:
                pass
        self.stats["swapped"] = swapped
        self.stats["waiting"] = sum(1 for v in self.state.values() if v == "waiting")

    def restore_all(self) -> int:
        """Put every original back (when Patchwork quits). Returns how many entries were restored."""
        with self._lock:
            self.swaps = []
            self._dirty = True
            if not self.store.exists() or not self.backups.manifest:
                return 0
            before = len(self.backups.manifest)
            try:
                conn = self._conn or self.store.connect()
                self._apply(conn)
            finally:
                self.close()
            return before - len(self.backups.manifest)


class _UiLogHandler(logging.Handler):
    """Forwards Patchwork's own log messages to the Activity tab."""

    def __init__(self, sink: Callable[[str, str], None]):
        super().__init__(logging.INFO)
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        if not record.name.startswith("patchwork"):
            return
        try:
            self.sink(record.levelname, record.getMessage())
        except Exception:
            pass


class CacheRunner:
    """Runs the cache swapper on a background thread while Patchwork is on."""

    def __init__(self, config, app_dir: Path, on_hit: Callable[[Hit], None], on_log: Callable[[str, str], None],
                 cache_dir: Path | None = None, interval: float = 2.0):
        directory = cache_dir or cache.default_dir() or (app_dir / "no-roblox-cache")
        self.swapper = CacheSwapper(cache.Store(directory), app_dir, Downloads(app_dir / "downloads"), on_hit)
        self.swapper.set_swaps(config.swaps)
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        handler = _UiLogHandler(on_log)
        logger = logging.getLogger("patchwork")
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def store(self) -> cache.Store:
        return self.swapper.store

    def update_config(self, config) -> None:
        self.swapper.set_swaps(config.swaps)

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="patchwork-cache", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.swapper.tick()
            self._stop.wait(self.interval)
        self.swapper.close()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(10)
        self._thread = None

    def restore_all(self) -> int:
        return self.swapper.restore_all()
