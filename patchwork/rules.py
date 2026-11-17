"""Rewrite Roblox asset-delivery requests and responses.

This module has no mitmproxy dependency so it can be tested anywhere. The
proxy addon in ``proxy.py`` feeds it decoded request and response bodies.

Roblox resolves asset IDs through assetdelivery.roblox.com, which answers with
the CDN URL to download from. Patchwork swaps assets at that step:

* an "asset" swap changes the requested ID, so Roblox downloads the
  replacement asset instead;
* a "file" or "url" swap keeps the request but points the answer at a URL on
  the same host under ``/patchwork/file/``, which the addon answers from disk
  (downloading web links first, so Roblox only ever talks to its own servers).
"""

from __future__ import annotations

import hashlib
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

from .config import KIND_ASSET, Swap

FILE_PREFIX = "/patchwork/file/"

# GET endpoints that take ?id=<asset id>. The v1 forms answer with a redirect
# to the CDN, the v2 form answers with JSON locations like the batch endpoint.
_QUERY_REDIRECT_PATHS = {"/v1/asset", "/asset"}
_QUERY_JSON_PATHS = {"/v2/asset"}
_PATH_JSON = re.compile(r"^/v[12]/assetid/(\d+)(/.*)?$", re.IGNORECASE)
_BATCH_PATHS = {"/v1/assets/batch", "/v2/assets/batch"}

_EXTRA_TYPES = {
    ".ogg": "audio/ogg",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".png": "image/png",
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tga": "image/x-tga",
    ".mesh": "application/octet-stream",
    ".rbxm": "application/octet-stream",
    ".rbxmx": "application/xml",
}


def content_type_for(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in _EXTRA_TYPES:
        return _EXTRA_TYPES[ext]
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


@dataclass(frozen=True)
class Hit:
    """One swap applied to one request, reported to the UI."""

    asset_id: int
    swap_name: str
    replacement: str


class SwapTable:
    """Fast lookup from original asset ID to the enabled swap that replaces it."""

    def __init__(self, swaps: list[Swap]):
        self._by_id: dict[int, Swap] = {}
        self._served: dict[str, Swap] = {}
        for swap in swaps:
            if not swap.enabled:
                continue
            for target in swap.targets:
                # Earlier swaps win if two swaps claim the same asset.
                self._by_id.setdefault(target, swap)
            if swap.kind != KIND_ASSET:
                self._served.setdefault(file_token(swap.value), swap)

    def __len__(self) -> int:
        return len(self._by_id)

    def lookup(self, asset_id: int) -> Swap | None:
        return self._by_id.get(asset_id)

    def served_for_token(self, token: str) -> Swap | None:
        """The file or link swap that a ``/patchwork/file/<token>`` URL stands for."""
        return self._served.get(token)

    def url_swaps(self) -> list[Swap]:
        return [s for s in self._served.values() if s.kind != KIND_ASSET and s.value.startswith("http")]


def file_token(source: str) -> str:
    """Short stable name for a replacement file path or link."""
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]


def file_url(host: str, source: str) -> str:
    return f"https://{host}{FILE_PREFIX}{file_token(source)}"


def _as_asset_id(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return None


def _item_key(item: dict, index: int) -> str:
    request_id = item.get("requestId") if isinstance(item, dict) else None
    return str(request_id) if request_id is not None else f"#{index}"


def is_batch_path(path: str) -> bool:
    return path.split("?", 1)[0].rstrip("/").lower() in _BATCH_PATHS


def rewrite_batch_request(
    items: list, table: SwapTable
) -> tuple[bool, dict[str, str], list[Hit]]:
    """Apply swaps to a batch request body in place.

    Returns whether the body changed, a map of item key -> replacement file
    path for "file" swaps (needed to rewrite the response), and the hits.
    """
    changed = False
    file_items: dict[str, str] = {}
    hits: list[Hit] = []
    if not isinstance(items, list):
        return changed, file_items, hits
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        asset_id = _as_asset_id(item.get("assetId"))
        if asset_id is None:
            continue
        swap = table.lookup(asset_id)
        if swap is None:
            continue
        if swap.kind == KIND_ASSET:
            original = item["assetId"]
            item["assetId"] = str(swap.value) if isinstance(original, str) else int(swap.value)
            changed = True
        else:
            file_items[_item_key(item, index)] = swap.value
        hits.append(Hit(asset_id, swap.name, swap.describe()))
    return changed, file_items, hits


def _point_at_file(entry: dict, host: str, path: str) -> None:
    entry["locations"] = [{"assetFormat": "source", "location": file_url(host, path)}]
    entry.pop("errors", None)


def rewrite_batch_response(items: list, file_items: dict[str, str], host: str) -> bool:
    """Point the answers for "file" swaps at Patchwork's local file URL."""
    if not file_items or not isinstance(items, list):
        return False
    changed = False
    for index, entry in enumerate(items):
        if not isinstance(entry, dict):
            continue
        path = file_items.get(_item_key(entry, index))
        if path is None:
            continue
        _point_at_file(entry, host, path)
        changed = True
    return changed


def rewrite_json_response(body: object, host: str, path: str) -> bool:
    """Rewrite a single-asset JSON answer (v2/asset, v1|v2/assetId/<id>)."""
    if not isinstance(body, dict):
        return False
    _point_at_file(body, host, path)
    return True


@dataclass
class SingleDecision:
    """What to do with a single-asset GET request."""

    hit: Hit
    swap: Swap
    new_path: str | None = None  # rewritten path + query for asset swaps
    serve_file: bool = False  # answer the request directly from disk
    rewrite_json: bool = False  # let it through, rewrite the JSON answer


def decide_single(path_and_query: str, table: SwapTable) -> SingleDecision | None:
    """Work out how to handle a single-asset GET such as /v1/asset/?id=123."""
    path, _, query = path_and_query.partition("?")
    norm = path.rstrip("/").lower()

    if norm in _QUERY_REDIRECT_PATHS or norm in _QUERY_JSON_PATHS:
        params = parse_qsl(query, keep_blank_values=True)
        id_index = next((i for i, (k, _) in enumerate(params) if k.lower() == "id"), None)
        if id_index is None:
            return None
        asset_id = _as_asset_id(params[id_index][1])
        swap = table.lookup(asset_id) if asset_id is not None else None
        if swap is None:
            return None
        hit = Hit(asset_id, swap.name, swap.describe())
        if swap.kind == KIND_ASSET:
            params[id_index] = (params[id_index][0], swap.value)
            return SingleDecision(hit, swap, new_path=f"{path}?{urlencode(params)}")
        if norm in _QUERY_REDIRECT_PATHS:
            return SingleDecision(hit, swap, serve_file=True)
        return SingleDecision(hit, swap, rewrite_json=True)

    match = _PATH_JSON.match(path)
    if match:
        asset_id = int(match.group(1))
        swap = table.lookup(asset_id)
        if swap is None:
            return None
        hit = Hit(asset_id, swap.name, swap.describe())
        if swap.kind == KIND_ASSET:
            start, end = match.span(1)
            new_path = path[:start] + swap.value + path[end:]
            return SingleDecision(hit, swap, new_path=new_path + (f"?{query}" if query else ""))
        return SingleDecision(hit, swap, rewrite_json=True)

    return None
