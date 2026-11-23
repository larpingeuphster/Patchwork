"""Import and export swap lists as JSON presets.

Two formats are understood:

* Patchwork's own: ``{"patchwork_preset": 1, "swaps": [...]}`` (the same swap
  fields as config.json; a full config.json imports too).
* Fleasion's, old and rewrite (``{"replacement_rules": [...]}`` or a bare list of
  rules): rules have ``name``, ``replace_ids``, ``enabled`` and a ``mode`` of
  ``"id"`` (``with_id``), ``"cdn"`` (``cdn_url``), ``"local"`` (``local_path``) or
  ``"remove"``; groups are ``{"type": "group", "children": [...]}``. Local paths
  like ``/Folder/file.obj`` point into the configs folder the JSON came from.
  Whole configs folders (the rewrite's ``configs/``) import one profile per file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .config import KIND_ASSET, KIND_FILE, KIND_REMOVE, KIND_URL, Swap

PRESET_VERSION = 1
FORMAT_PATCHWORK = "Patchwork"
FORMAT_FLEASION = "Fleasion"


@dataclass
class ImportResult:
    format: str
    swaps: list[Swap] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    missing_files: list[str] = field(default_factory=list)  # local files not on this PC

    def link_hosts(self) -> list[str]:
        return sorted({urlparse(s.value).netloc for s in self.swaps if s.kind == KIND_URL})


def _rule_name(rule: dict, index: int) -> str:
    name = rule.get("name")
    return str(name).strip() if isinstance(name, str) and name.strip() else f"Rule {index + 1}"


def _ids(value) -> list:
    if isinstance(value, (int, str)):
        return [value]
    return list(value) if isinstance(value, list) else []


def fleasion_configs_dir() -> Path | None:
    """The new Fleasion's configs folder on this PC (%LOCALAPPDATA%\\FleasionNT\\configs), if it exists."""
    import os
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    folder = Path(local) / "FleasionNT" / "configs"
    return folder if folder.is_dir() else None


def _resolve_local(path: str, base_dir: Path | None) -> Path:
    """A Fleasion local_path. ``/Folder/file.ext`` means a file in the configs folder the JSON came
    from (or Fleasion's own configs folder); anything else is a normal path on this PC."""
    raw = path.replace("\\", "/")
    parts = [p for p in raw.split("/") if p]
    portable = raw.startswith("/") and len(parts) >= 2 and not any(p in (".", "..") for p in parts) \
        and ":" not in parts[0]
    candidates = []
    if portable:
        for root in (base_dir, fleasion_configs_dir()):
            if root is not None:
                candidates.append(root.joinpath(*parts))
    candidates.append(Path(path))
    for c in candidates:
        if c.is_file():
            return c
    return candidates[0]


def _fleasion_targets(rule: dict, name: str, result: ImportResult) -> list[int]:
    targets = []
    for raw in _ids(rule.get("replace_ids")):
        if isinstance(raw, bool):
            continue
        text = str(raw).strip()
        if text.isdigit():
            targets.append(int(text))
        elif text:
            # Asset-type words ("Image", "TexturePack"...) mean "every asset of that type"; Patchwork
            # swaps specific files, so those can't be applied.
            result.skipped.append(f'{name}: target "{text}" (a whole asset type) isn\'t supported')
    return targets


def _fleasion_rule(rule: dict, name: str, result: ImportResult, base_dir: Path | None = None) -> None:
    mode = str(rule.get("mode", "")).strip().lower()
    if not mode:
        # The rewrite defaults to "id"; older files used a remove flag instead of a mode.
        mode = "remove" if rule.get("remove") is True else "id"
    try:
        targets = _fleasion_targets(rule, name, result)
        if not targets:
            if not any(r.startswith(f"{name}:") for r in result.skipped):
                result.skipped.append(f"{name}: no asset IDs to replace")
            return
        enabled = rule.get("enabled", True) is not False
        if rule.get("filters"):
            result.skipped.append(f"{name}: its size/length filters were ignored (the swap applies to its IDs "
                                  "regardless)")
        if mode == "remove":
            swap = Swap(name, targets, KIND_REMOVE, "", enabled)
        elif mode == "id":
            with_id = str(rule.get("with_id") or "").strip()
            if with_id in ("", "0", "-1", "None"):
                # An ID rule with nothing to replace it with removes the asset.
                swap = Swap(name, targets, KIND_REMOVE, "", enabled)
            elif with_id.isdigit() and int(with_id) <= 80:
                result.skipped.append(f"{name}: with_id {with_id} isn't an asset ID")
                return
            else:
                swap = Swap(name, targets, KIND_ASSET, with_id, enabled)
        elif mode == "cdn":
            url = str(rule.get("cdn_url") or "").strip()
            swap = Swap(name, targets, KIND_URL, url, enabled) if url else Swap(name, targets, KIND_REMOVE, "", enabled)
        elif mode == "local":
            path = str(rule.get("local_path") or "").strip()
            if not path:
                swap = Swap(name, targets, KIND_REMOVE, "", enabled)
            else:
                found = _resolve_local(path, base_dir)
                swap = Swap(name, targets, KIND_FILE, str(found), enabled)
                if not found.is_file():
                    result.missing_files.append(f"{name}: {path}")
        else:
            result.skipped.append(f'{name}: mode "{mode}" isn\'t supported yet')
            return
    except (ValueError, TypeError) as exc:
        result.skipped.append(f"{name}: {exc}")
        return
    result.swaps.append(swap)


def _fleasion_rules(rules: list, result: ImportResult, prefix: str = "", depth: int = 0,
                    base_dir: Path | None = None) -> None:
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            result.skipped.append(f"{prefix}Rule {index + 1}: not a rule")
            continue
        name = prefix + _rule_name(rule, index)
        if rule.get("type") == "group" or "children" in rule:
            children = rule.get("children")
            if isinstance(children, list) and depth < 10:
                # Groups are folders of rules; their names prefix the rules inside.
                _fleasion_rules(children, result, f"{name} / ", depth + 1, base_dir)
            else:
                result.skipped.append(f"{name}: empty or unreadable group")
            continue
        _fleasion_rule(rule, name, result, base_dir)


def parse_fleasion(data, base_dir: Path | None = None) -> ImportResult:
    result = ImportResult(FORMAT_FLEASION)
    rules = data if isinstance(data, list) else data.get("replacement_rules")
    if not isinstance(rules, list):
        raise ValueError("replacement_rules should be a list")
    _fleasion_rules(rules, result, base_dir=base_dir)
    return result


def parse_patchwork(data: dict, base_dir: Path | None = None) -> ImportResult:
    result = ImportResult(FORMAT_PATCHWORK)
    swaps = data.get("swaps")
    if not isinstance(swaps, list):
        raise ValueError("swaps should be a list")
    for index, raw in enumerate(swaps):
        if not isinstance(raw, dict):
            result.skipped.append(f"Swap {index + 1}: not a swap")
            continue
        name = _rule_name(raw, index)
        try:
            fields = {k: raw[k] for k in ("name", "targets", "kind", "value", "enabled") if k in raw}
            fields["name"] = name
            swap = Swap(**fields)
        except (ValueError, TypeError) as exc:
            result.skipped.append(f"{name}: {exc}")
            continue
        # Presets can ship their files next to the JSON and refer to them relatively.
        if swap.kind == KIND_FILE and base_dir is not None and not Path(swap.value).is_absolute():
            swap.value = str((base_dir / swap.value).resolve())
        result.swaps.append(swap)
    return result


def parse_preset(data: object, base_dir: Path | None = None) -> ImportResult:
    if isinstance(data, list) and all(isinstance(r, dict) for r in data):
        return parse_fleasion(data, base_dir)  # the rewrite accepts a bare list of rules
    if not isinstance(data, dict):
        raise ValueError("This file isn't a swap preset.")
    if "replacement_rules" in data:
        return parse_fleasion(data, base_dir)
    if "swaps" in data:
        return parse_patchwork(data, base_dir)
    raise ValueError("This file isn't a Patchwork or Fleasion preset (no swaps or replacement_rules).")


def _read_json(path: Path):
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        encodings = ["utf-16"]
    else:
        encodings = ["utf-8-sig", "cp1252"]
    error = None
    for enc in encodings:
        try:
            return json.loads(raw.decode(enc))
        except UnicodeDecodeError as exc:
            error = exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"This file isn't valid JSON (line {exc.lineno}): {exc.msg}") from exc
    raise ValueError(f"This file's text encoding can't be read: {error}")


def load_folder(folder: Path) -> tuple[list[tuple[str, ImportResult]], list[str]]:
    """Every config in a Fleasion configs folder: (name, result) per root .json, plus the
    names Fleasion has switched on (from settings.json beside the folder, if there is one)."""
    configs = []
    for path in sorted(folder.glob("*.json"), key=lambda p: p.name.lower()):
        try:
            configs.append((path.stem, load_preset(path)))
        except (ValueError, OSError) as exc:
            empty = ImportResult(FORMAT_FLEASION)
            empty.skipped.append(f"{path.name}: {exc}")
            configs.append((path.stem, empty))
    enabled: list[str] = []
    try:
        settings = _read_json(folder.parent / "settings.json")
        if isinstance(settings, dict) and isinstance(settings.get("enabled_configs"), list):
            enabled = [str(n) for n in settings["enabled_configs"]]
    except (ValueError, OSError):
        pass
    return configs, enabled


def load_preset(path: Path) -> ImportResult:
    data = _read_json(path)
    return parse_preset(data, path.parent)


def _same(a: Swap, b: Swap) -> bool:
    return (a.kind, a.value, sorted(a.targets)) == (b.kind, b.value, sorted(b.targets))


def merge(existing: list[Swap], incoming: list[Swap]) -> tuple[list[Swap], int]:
    """Swaps from ``incoming`` that aren't already in ``existing``, and how many were duplicates."""
    added, dupes = [], 0
    for swap in incoming:
        if any(_same(swap, other) for other in existing + added):
            dupes += 1
        else:
            added.append(swap)
    return added, dupes


def export_patchwork(swaps: list[Swap]) -> str:
    data = {
        "patchwork_preset": PRESET_VERSION,
        "swaps": [
            {"name": s.name, "targets": s.targets, "kind": s.kind, "value": s.value, "enabled": s.enabled}
            for s in swaps
        ],
    }
    return json.dumps(data, indent=2)


def export_fleasion(swaps: list[Swap]) -> tuple[str, list[str]]:
    """Fleasion-format JSON, plus names of swaps it can't express (none at the moment)."""
    rules, skipped = [], []
    for s in swaps:
        rule = {"name": s.name, "replace_ids": s.targets}
        if s.kind == KIND_ASSET:
            rule.update(mode="id", enabled=s.enabled, with_id=int(s.value))
        elif s.kind == KIND_REMOVE:
            rule.update(mode="id", enabled=s.enabled)
        elif s.kind == KIND_URL:
            rule.update(mode="cdn", enabled=s.enabled, cdn_url=s.value)
        elif s.kind == KIND_FILE:
            rule.update(mode="local", enabled=s.enabled, local_path=s.value)
        else:
            skipped.append(s.name)
            continue
        rules.append(rule)
    return json.dumps({"replacement_rules": rules}, indent=2), skipped
