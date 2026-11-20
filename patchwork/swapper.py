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
import logging
import re
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

_BLOB_NAME = re.compile(r"[0-9a-f]{32}")
_HASH_IN_URL = re.compile(r"/([0-9a-f]{32})(?:/|$)")

ASSET_URL = "https://assetdelivery.roblox.com/v1/asset/?id={id}"
MAX_ASSET = 50 * 1024 * 1024
RETRY_REFUSED_AFTER = 6 * 60 * 60  # Roblox said no (sign-in needed, missing asset)
RETRY_NETWORK_AFTER = 2 * 60  # couldn't reach Roblox: try again soon
IMAGE_KINDS = {"png", "jpeg", "gif", "webp"}
MATCH_MAX = 14.0  # mean channel difference (0-255) of the 16x16 thumbnails for a texture match
MATCH_MARGIN = 4.0  # ...and how much better than the next best it must be
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
    sig: bytes = b""  # tiny thumbnail, for finding the texture's KTX2 copy by its look
    width: int = 0
    height: int = 0


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
        if rec and "md5" in rec and (rec.get("kind") not in IMAGE_KINDS or "sig" in rec or rec.get("nosig")):
            return Fingerprint(rec["md5"], rec["size"], rec.get("kind", "other"), bytes.fromhex(rec.get("sig", "")),
                               rec.get("w", 0), rec.get("h", 0))
        if rec and now - rec.get("at", 0) < (RETRY_NETWORK_AFTER if rec.get("transient") else RETRY_REFUSED_AFTER):
            return rec.get("error", "unavailable")
        try:
            body = self.fetch(asset_id)
        except Unavailable as exc:
            self._data[str(asset_id)] = {"error": str(exc), "at": now, "transient": exc.transient}
            self.save()
            return str(exc)
        fp = Fingerprint(cache.md5(body), len(body), cache.sniff(body))
        rec = {"md5": fp.md5, "size": fp.size, "kind": fp.kind, "at": now}
        if fp.kind in IMAGE_KINDS:
            try:
                from PIL import Image

                from . import ktx2
                img = Image.open(io.BytesIO(body))
                img.load()
                fp.sig, fp.width, fp.height = ktx2.signature(img), img.width, img.height
                rec.update(sig=fp.sig.hex(), w=fp.width, h=fp.height)
            except Exception:
                rec["nosig"] = True
        self._data[str(asset_id)] = rec
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


def _png(img) -> bytes:
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def to_ktx2_like(data: bytes, original: bytes) -> bytes:
    """``data`` (any image Pillow reads, or a KTX2) as a KTX2 texture shaped like ``original``."""
    from . import ktx2
    try:
        like = ktx2.parse(original)
    except ktx2.KtxError as exc:
        raise Unavailable(f"the cached texture can't be read ({exc})") from exc
    try:
        from PIL import Image
        if data.startswith(ktx2.MAGIC):
            img = ktx2.decode(ktx2.parse(data))
        else:
            img = Image.open(io.BytesIO(data))
            img.load()
    except ImportError as exc:
        raise Unavailable("image conversion isn't available in this build") from exc
    except Exception as exc:
        raise Unavailable("the replacement isn't an image Patchwork can read") from exc
    try:
        return ktx2.encode_like(img, like)
    except Exception as exc:
        raise Unavailable(f"couldn't convert the replacement to a {like.describe()} texture: {exc}") from exc


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
        self.captures: list = []  # capture.Capture, applied as copies of captured cache files
        self.captured_body: Callable[[str], bytes | None] = lambda md5: None
        self._lock = threading.RLock()
        self._last_version: int | None = None
        self._dirty = True
        # Keyed by (id, size, atime) so an entry Roblox rewrites is read again.
        self._body_info: dict[tuple[bytes, int, int], tuple[int, str]] = {}
        self._md5: dict[tuple[bytes, int, int], str] = {}
        self._looks: dict[tuple[bytes, int, int], tuple[bytes, int, int] | str] = {}
        self._conn = None  # kept open: SQLite's data_version only compares within one connection
        self._replacements: dict[tuple[str, str], bytes] = {}
        self._reported: set[str] = set()
        self._last_unavailable: tuple = ()
        self._described = False
        self._matched_note_done = False
        self.stats = {"swapped": 0, "waiting": 0, "unavailable": 0}
        self.state: dict[int, str] = {}  # asset id -> "swapped" / "waiting" / reason

    # --- configuration ---------------------------------------------------------------------

    def set_swaps(self, swaps: list[Swap]) -> None:
        with self._lock:
            self.swaps = [s for s in swaps if s.enabled]
            self._replacements = {k: v for k, v in self._replacements.items()
                                  if any(swap_key(s) == k[0] for s in self.swaps)}
            self._dirty = True

    def set_captures(self, captures: list, body: Callable[[str], bytes | None]) -> None:
        with self._lock:
            self.captures = list(captures)
            self.captured_body = body
            self._dirty = True

    # --- skin capture ------------------------------------------------------------------------

    def capture_mark(self) -> dict[bytes, tuple[int, int, int]]:
        """What the cache looks like now, to compare against after showing a skin in game."""
        with self._lock:
            if not self.store.exists():
                return {}
            conn = self._conn or self.store.connect()
            self._conn = conn
            return {e.id: (e.size, e.atime, e.hits) for e in self.store.entries(conn)}

    def capture_seen(self, mark: dict[bytes, tuple[int, int, int]]) -> list:
        """Entries Roblox added, rewrote or read since ``mark`` (skipping ones holding swaps)."""
        from . import capture
        with self._lock:
            if not self.store.exists():
                return []
            conn = self._conn or self.store.connect()
            self._conn = conn
            out = []
            for e in self.store.entries(conn):
                if mark.get(e.id) == (e.size, e.atime, e.hits) or e.hex in self.backups.manifest:
                    continue
                if e.category == cache.PHYSICS_CATEGORY:
                    continue
                info = self._info(conn, e)
                if info.kind not in capture.GROUPS:
                    continue
                pv = ""
                if info.kind in capture.PREVIEWABLE and info.body_len <= capture.PREVIEW_MAX:
                    try:
                        pv = capture.preview(info.kind, self.store.read_body(conn, e.id)[0])
                    except (OSError, KeyError):
                        pass
                out.append(capture.Seen(e.hex, info.kind, info.body_len, capture._label(info.url, info.kind), pv))
            return sorted(out, key=lambda s: (s.kind, -s.size))

    def browse(self) -> list[dict]:
        """Every cached file Patchwork can show, newest first (for the Cache tab)."""
        from . import capture
        with self._lock:
            if not self.store.exists():
                return []
            conn = self._conn or self.store.connect()
            self._conn = conn
            out = []
            for e in self.store.entries(conn):
                if e.category == cache.PHYSICS_CATEGORY:
                    continue
                info = self._info(conn, e)
                if info.kind not in capture.GROUPS:
                    continue
                out.append({"hex": e.hex, "kind": info.kind, "size": info.body_len, "atime": e.atime,
                            "hits": e.hits})
            out.sort(key=lambda r: (-r["atime"], r["hex"]))
            return out

    def preview_entry(self, entry_hex: str) -> dict:
        """A data: URL to show (images, decoded textures) or play (sounds), if there is one."""
        import base64

        from . import capture, ktx2
        body = self.read_entry(entry_hex)
        if body is None:
            return {}
        kind = cache.sniff(body)
        info: dict = {"kind": kind}
        if kind in capture.PREVIEWABLE:
            info["image"] = f"data:{capture.PREVIEWABLE[kind]};base64,{base64.b64encode(body).decode()}"
            try:
                from PIL import Image
                info["detail"] = "%dx%d" % Image.open(io.BytesIO(body)).size
            except Exception:
                pass
        elif kind == "ktx2":
            try:
                tex = ktx2.parse(body)
                info["detail"] = tex.describe()
                img = ktx2.decode(tex, ktx2.preview_level(tex, 256))
                info["image"] = "data:image/png;base64," + base64.b64encode(_png(img)).decode()
            except Exception as exc:
                info["detail"] = f"can't preview ({exc})"
        elif kind == "mesh":
            from . import mesh
            try:
                m = mesh.parse(body)
                info["detail"] = m.describe()
                info["image"] = "data:image/png;base64," + base64.b64encode(mesh.render(m, 160)).decode()
                info["mesh"] = True
            except Exception as exc:
                info["detail"] = f"can't preview ({exc})"
        elif kind in capture.AUDIO and len(body) <= 8 << 20:
            info["audio"] = f"data:{capture.AUDIO[kind]};base64,{base64.b64encode(body).decode()}"
        return info

    def mesh_entry(self, entry_hex: str) -> dict:
        """A cached mesh's shape for the interactive 3D viewer."""
        from . import mesh
        body = self.read_entry(entry_hex)
        if body is None:
            return {}
        try:
            return mesh.for_viewer(mesh.parse(body))
        except mesh.MeshError as exc:
            return {"error": str(exc)}

    def replacement_for(self, entry_hex: str, data: bytes) -> bytes:
        """``data`` (a file the player picked) made to fit the cache entry, or Refused/Unavailable."""
        original = self.read_entry(entry_hex)
        if original is None:
            raise Unavailable("Roblox no longer has that file in its cache")
        kind = cache.sniff(original)
        if kind == "ktx2":
            return to_ktx2_like(data, original)
        if kind in IMAGE_KINDS:
            if cache.sniff(data) not in IMAGE_KINDS and not data.startswith(b"\xabKTX"):
                raise Unavailable("pick an image (PNG, JPG or WEBP) to replace an image")
            if data.startswith(b"\xabKTX"):
                from . import ktx2
                return _png(ktx2.decode(ktx2.parse(data)))
            return fit_image(data, original)
        new = cache.sniff(data)
        if kind in ("ogg", "mp3", "wav") and new != kind:
            raise Unavailable(f"pick a .{kind} sound to replace this one (Roblox keeps the format)")
        return data

    def read_entry(self, entry_hex: str) -> bytes | None:
        with self._lock:
            conn = self._conn or self.store.connect()
            self._conn = conn
            try:
                return self.store.read_body(conn, bytes.fromhex(entry_hex))[0]
            except (OSError, KeyError, ValueError):
                return None

    def report(self) -> list[str]:
        """What's in the cache: per entry its id, kind, size, category, status, address and redirect
        target (addresses without their access tokens). The top lines test how Roblox names cache
        entries, which decides how textures can be matched. Nothing personal is included."""
        import hashlib
        with self._lock:
            if not self.store.exists():
                return ["Roblox's cache wasn't found."]
            conn = self._conn or self.store.connect()
            self._conn = conn
            entries = self.store.entries(conn)
            ids = {e.id for e in entries}
            infos = {e.id: self._info(conn, e) for e in entries}
            tests: dict[str, int] = {}

            def check(name: str, candidate: bytes | None) -> None:
                tests.setdefault(name, 0)
                if candidate is not None and candidate in ids:
                    tests[name] += 1

            redirects = 0
            for e in entries:
                info = infos[e.id]
                if not info.url:
                    continue
                if 300 <= info.status < 400:
                    redirects += 1
                for label, url in (("url", info.url), ("location", info.location)):
                    if not url:
                        continue
                    bare = url.split("?", 1)[0]
                    m = _HASH_IN_URL.search(bare)
                    check(f"{label}: id == hash in address", bytes.fromhex(m.group(1)) if m else None)
                    for variant, text in (("full", url), ("no query", bare)):
                        data = text.encode()
                        check(f"{label}: id == md5({variant})", hashlib.md5(data).digest())
                        check(f"{label}: id == sha1({variant})[:16]", hashlib.sha1(data).digest()[:16])
                        check(f"{label}: id == sha256({variant})[:16]", hashlib.sha256(data).digest()[:16])
                        try:
                            import xxhash
                            check(f"{label}: id == xxh128({variant})", xxhash.xxh128(data).digest())
                            check(f"{label}: id == xxh3_128({variant})", xxhash.xxh3_128(data).digest())
                        except (ImportError, AttributeError):
                            pass
            lines = [f"Patchwork {__version__} cache report: {len(entries)} entries, {redirects} redirects",
                     "Naming tests (how many entries each rule finds):"]
            lines += [f"  {n}\t{name}" for name, n in sorted(tests.items(), key=lambda kv: -kv[1])]
            from . import ktx2
            formats: dict[str, int] = {}
            described = {}
            for e in entries:
                if infos[e.id].kind == "ktx2":
                    try:
                        tex = ktx2.parse(self.store.read_body(conn, e.id)[0])
                        described[e.id] = tex.describe() + ("" if tex.readable else " (can't read)")
                        key = f"{tex.format_name}/{ktx2.SUPERCOMPRESSION.get(tex.supercompression)}"
                    except Exception as exc:
                        described[e.id] = key = f"unreadable: {exc}"
                    formats[key] = formats.get(key, 0) + 1
            lines += ["Texture formats:"] + [f"  {n}\t{k}" for k, n in sorted(formats.items(), key=lambda kv: -kv[1])]
            lines += ["", "id\tkind\tbody_bytes\tcategory\tstatus\turl\tlocation\ttexture"]
            for e in entries:
                info = infos[e.id]
                lines.append(f"{e.hex}\t{info.kind}\t{info.body_len}\t{e.category}\t{info.status}\t"
                             f"{(info.url or '').split('?', 1)[0]}\t{(info.location or '').split('?', 1)[0]}\t"
                             f"{described.get(e.id, '')}")
            return lines

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
            if kind == "ktx2":
                from PIL import Image
                data = to_ktx2_like(_png(Image.new("RGBA", (4, 4), (0, 0, 0, 0))), original)
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
                data = to_ktx2_like(data, original)
            elif kind in IMAGE_KINDS:
                data = fit_image(data, original)
        self._replacements[key] = data
        return data

    def _look(self, conn, entry: cache.Entry) -> tuple[bytes, int, int] | str:
        """(thumbnail, width, height) of a cached KTX2 texture, or why it can't be read."""
        key = (entry.id, entry.size, entry.atime)
        if key not in self._looks:
            from . import ktx2
            try:
                body, _ = self.store.read_body(conn, entry.id)
                tex = ktx2.parse(body)
                if not tex.readable:
                    self._looks[key] = tex.describe()
                else:
                    self._looks[key] = (ktx2.signature(ktx2.decode(tex, ktx2.preview_level(tex))), tex.width,
                                        tex.height)
            except Exception as exc:
                self._looks[key] = f"unreadable ({type(exc).__name__})"
        return self._looks[key]

    def _match_textures(self, conn, entries: list[cache.Entry], linked: dict) -> dict[str, tuple[int, Swap]]:
        """Cached KTX2 textures that look like a wanted image -> (asset id, swap).

        Roblox only hands the KTX2 version of a texture to signed-in players, so its bytes never
        match the PNG Patchwork can download. Instead each cached texture is decoded and compared
        with the PNG as a 16x16 thumbnail. A match must be close and clearly better than the next
        best, so plain or look-alike textures are left alone rather than guessed.
        """
        from . import ktx2
        targets = []
        taken = {aid for aid, _ in linked.values()}
        for swap in self.swaps:
            for asset_id in swap.targets:
                if asset_id in taken:
                    continue
                fp = self.idmap.get(asset_id)
                if isinstance(fp, Fingerprint) and fp.kind in IMAGE_KINDS and fp.sig:
                    targets.append((asset_id, swap, fp))
                    taken.add(asset_id)
        if not targets:
            return {}
        looks = []
        unreadable: dict[str, int] = {}
        for entry in entries:
            if self._info(conn, entry).kind != "ktx2" or entry.hex in linked:
                continue
            rec = self.backups.manifest.get(entry.hex)
            if rec and self._entry_md5(conn, entry) == rec.get("written_md5"):
                continue  # holds a replacement already; the main loop counts it
            look = self._look(conn, entry)
            if isinstance(look, str):
                unreadable[look.split(" ", 1)[0]] = unreadable.get(look.split(" ", 1)[0], 0) + 1
                continue
            looks.append((entry.hex, *look))
        if unreadable:
            summary = ", ".join(f"{n} {k}" for k, n in sorted(unreadable.items()))
            self._note(f"ktx-unreadable:{summary}", f"Some cached textures use a format Patchwork can't read yet "
                       f"({summary}); texture swaps for those won't apply.", logging.INFO)
        found: dict[str, tuple[float, int, Swap]] = {}
        for asset_id, swap, fp in targets:
            scored = sorted((ktx2.distance(fp.sig, sig), hx) for hx, sig, w, h in looks
                            if ktx2.same_shape(fp.width, fp.height, w, h))
            if not scored:
                continue
            best, hx = scored[0]
            runner_up = scored[1][0] if len(scored) > 1 else 255.0
            if best > MATCH_MAX or runner_up - best < MATCH_MARGIN:
                continue
            if hx not in found or best < found[hx][0]:
                found[hx] = (best, asset_id, swap)
        if found and not self._matched_note_done:
            self._matched_note_done = True
            log.info("Matched %d texture(s) to Roblox's cached KTX2 copies by how they look.", len(found))
        return {hx: (aid, swap) for hx, (_, aid, swap) in found.items()}

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
        for memo in (self._md5, self._body_info, self._looks):
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
        textures = self._match_textures(conn, entries, linked)
        sizes = {size for size, _ in wanted}
        present = {e.hex: e for e in entries}
        active_keys = {swap_key(s) for s in self.swaps}
        captured: dict[str, tuple[str, str]] = {}  # entry hex -> (capture key, replacement md5)
        for cap in self.captures:
            for pair in cap.pairs:
                captured.setdefault(pair.target, (cap.key, pair.source))

        # Put back entries whose swap is gone (or changed), if Roblox hasn't replaced them since.
        for entry_hex, rec in list(self.backups.manifest.items()):
            entry = present.get(entry_hex)
            target_still_wanted = any(rec.get("asset_id") == aid and swap_key(s) == rec.get("swap")
                                      for aid, s in list(wanted.values()) + list(linked.values()))
            if target_still_wanted and rec.get("swap") in active_keys:
                continue
            if captured.get(entry_hex, (None,))[0] == rec.get("swap"):
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
            if entry.hex in captured:
                continue  # handled by _apply_captures
            body_len = self._info(conn, entry).body_len
            rec = self.backups.manifest.get(entry.hex)
            if body_len not in sizes and rec is None and entry.hex not in linked and entry.hex not in textures:
                continue
            digest = self._entry_md5(conn, entry)
            if digest is None:
                continue
            if rec and digest == rec.get("written_md5"):
                swapped += 1  # still holds our replacement
                self.state[rec.get("asset_id")] = "swapped"
                continue
            match = linked.get(entry.hex) or textures.get(entry.hex) or wanted.get((body_len, digest))
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
        swapped += self._apply_captures(conn, present, captured)
        self.stats["swapped"] = swapped
        self.stats["waiting"] = sum(1 for v in self.state.values() if v == "waiting")

    def _apply_captures(self, conn, present: dict, captured: dict[str, tuple[str, str]]) -> int:
        done = 0
        applied: dict[str, int] = {}
        for entry_hex, (key, source) in captured.items():
            entry = present.get(entry_hex)
            if entry is None:
                continue  # Roblox hasn't got this file right now; it's swapped when it comes back
            digest = self._entry_md5(conn, entry)
            rec = self.backups.manifest.get(entry_hex)
            if digest is None:
                continue
            if digest == source or (rec and digest == rec.get("written_md5") and rec.get("swap") == key):
                done += 1
                continue
            body = self.captured_body(source)
            name = key.split(":", 1)[1]
            if body is None:
                self._note(f"capbody:{source}", f"The captured file for {name} is missing; capture it again.")
                continue
            try:
                original, _ = self.store.read_body(conn, entry.id)
                self.store.replace_body(conn, entry.id, body)
                self._forget(entry.id)
            except cache.Refused as exc:
                self._note(f"capfail:{entry_hex}:{source}", f"Can't apply {name}: {exc}.")
                continue
            except Exception as exc:
                self._note(f"caperr:{entry_hex}", f"Couldn't write {name} into Roblox's cache: {exc}")
                continue
            self.backups.keep(entry_hex, original, 0, body, key)
            done += 1
            applied.setdefault(name, 0)
            applied[name] += 1
        for name, n in applied.items():
            try:
                self.on_hit(Hit(0, name, f"{n} captured file(s)"))
            except Exception:
                pass
        return done

    def clear_keeping_swaps(self) -> tuple[int, int, int]:
        """Delete Roblox's cached assets except the ones holding swaps.

        Roblox looks assets up in its cache by address, so a swapped entry that stays is
        used again straight away after a clear, with no download and no wait. Works while
        Roblox is open: the index is shared through SQLite's own locking, and files Roblox
        has open are simply left for later. Returns (removed, kept, left_in_use).
        """
        with self._lock:
            if not self.store.exists():
                return 0, 0, 0
            conn = self._conn or self.store.connect()
            try:
                entries = self.store.entries(conn)
                keep = set()
                for entry in entries:
                    rec = self.backups.manifest.get(entry.hex)
                    if rec and self._entry_md5(conn, entry) == rec.get("written_md5"):
                        keep.add(entry.id)
                drop = [(e.id,) for e in entries if e.id not in keep]
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.executemany("DELETE FROM files WHERE id = ?", drop)
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
            finally:
                self.close()
            keep_names = {i.hex() for i in keep}
            in_use = 0
            if self.store.blobs.is_dir():
                for path in self.store.blobs.rglob("*"):
                    if not path.is_file() or path.name in keep_names:
                        continue
                    if not (_BLOB_NAME.fullmatch(path.name) or path.name.endswith(".tmp")):
                        continue
                    try:
                        path.unlink()
                    except OSError:
                        in_use += 1
            self._md5.clear()
            self._body_info.clear()
            self._looks.clear()
            self._described = False
            self._dirty = True
            return len(drop), len(keep), in_use

    def restore_all(self) -> int:
        """Put every original back (when Patchwork quits). Returns how many entries were restored."""
        with self._lock:
            self.swaps = []
            self.captures = []
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
                 cache_dir: Path | None = None, interval: float = 1.0):
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
