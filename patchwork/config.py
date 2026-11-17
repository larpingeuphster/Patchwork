"""Swap list and settings, stored as JSON in the user's app data folder."""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlparse

CONFIG_VERSION = 1

KIND_ASSET = "asset"  # replace with another Roblox asset ID
KIND_FILE = "file"  # replace with a file on this PC
KIND_URL = "url"  # replace with a file downloaded from a web link
KINDS = (KIND_ASSET, KIND_FILE, KIND_URL)

DEFAULT_INTERCEPT_HOSTS = ["assetdelivery.roblox.com"]
DEFAULT_PROCESSES = ["RobloxPlayerBeta.exe"]


def app_dir() -> Path:
    """Folder that holds Patchwork's config and certificate authority."""
    override = os.environ.get("PATCHWORK_HOME")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "Patchwork"
    return Path.home() / ".patchwork"


def config_path() -> Path:
    return app_dir() / "config.json"


@dataclass
class Swap:
    """Replace every asset in ``targets`` with another asset ID or a local file."""

    name: str
    targets: list[int]
    kind: str
    value: str
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"Unknown swap kind {self.kind!r}")
        self.targets = [int(t) for t in self.targets]
        if not self.targets:
            raise ValueError("A swap needs at least one asset ID to replace")
        if any(t <= 0 for t in self.targets):
            raise ValueError("Asset IDs must be positive numbers")
        self.value = str(self.value).strip()
        if self.kind == KIND_ASSET:
            if not self.value.isdigit() or int(self.value) <= 0:
                raise ValueError("The replacement asset ID must be a positive number")
        elif self.kind == KIND_URL:
            parsed = urlparse(self.value)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ValueError("The replacement link must start with https:// or http://")
        elif not self.value:
            raise ValueError("Choose a replacement file")

    def describe(self) -> str:
        if self.kind == KIND_ASSET:
            return f"asset {self.value}"
        if self.kind == KIND_URL:
            parsed = urlparse(self.value)
            name = parsed.path.rstrip("/").rsplit("/", 1)[-1]
            return f"{parsed.netloc}/{name}" if name else parsed.netloc
        return Path(self.value).name


@dataclass
class Config:
    swaps: list[Swap] = field(default_factory=list)
    intercept_hosts: list[str] = field(default_factory=lambda: list(DEFAULT_INTERCEPT_HOSTS))
    processes: list[str] = field(default_factory=lambda: list(DEFAULT_PROCESSES))

    def to_json(self) -> str:
        data = {"version": CONFIG_VERSION, **asdict(self)}
        return json.dumps(data, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "Config":
        data = json.loads(text)
        swaps = [Swap(**s) for s in data.get("swaps", [])]
        return cls(
            swaps=swaps,
            intercept_hosts=list(data.get("intercept_hosts") or DEFAULT_INTERCEPT_HOSTS),
            processes=list(data.get("processes") or DEFAULT_PROCESSES),
        )

    def save(self, path: Path | None = None) -> None:
        path = path or config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(self.to_json(), encoding="utf-8")
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or config_path()
        if not path.exists():
            return cls()
        return cls.from_json(path.read_text(encoding="utf-8"))


def parse_asset_ids(text: str) -> list[int]:
    """Parse "123, 456 789" or Roblox URLs into a list of asset IDs."""
    ids: list[int] = []
    for raw in text.replace(",", " ").split():
        # rbxassetid://123, ...?id=123, roblox.com/library/123/name, or a bare 123
        match = re.search(r"id=(\d+)", raw) or re.search(r"(\d+)", raw)
        if not match:
            raise ValueError(f"No asset ID found in {raw!r}")
        ids.append(int(match.group(1)))
    return ids


_ASSET_ID_INPUT = re.compile(r"(?:rbxassetid://)?(\d+)", re.IGNORECASE)


def swap_from_inputs(
    name: str, ids_text: str, replace_text: str, configs_dir: Path, enabled: bool = True
) -> Swap:
    """Build a swap from the editor's three boxes, working out the kind from "Replace with".

    "Replace with" takes an asset ID (or rbxassetid:// or a roblox.com link), a
    web link, a file path, or a path inside the configs folder written as
    ``/Folder/file.png``.
    """
    if not ids_text.strip():
        raise ValueError("Enter at least one asset ID to replace.")
    targets = parse_asset_ids(ids_text)
    value = replace_text.strip().strip('"').strip()
    if not value:
        raise ValueError("Enter what to replace them with: an asset ID, a link, or a file.")

    if match := _ASSET_ID_INPUT.fullmatch(value):
        kind, value = KIND_ASSET, match.group(1)
    elif value.lower().startswith(("http://", "https://")):
        host = urlparse(value).netloc.lower().split(":")[0]
        if host == "roblox.com" or host.endswith(".roblox.com"):
            kind, value = KIND_ASSET, str(parse_asset_ids(value)[0])
        else:
            kind = KIND_URL
    else:
        path = Path(value)
        inside = configs_dir / value.lstrip("/\\")
        if value.startswith(("/", "\\")) and inside.is_file():
            path = inside
        if not path.is_file():
            raise ValueError(f"Couldn't find the file {value}")
        kind, value = KIND_FILE, str(path.resolve())

    if not name.strip():
        name = Swap(name="x", targets=targets, kind=kind, value=value).describe()
    return Swap(name=name.strip(), targets=targets, kind=kind, value=value, enabled=enabled)
