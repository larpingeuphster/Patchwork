"""Import and export swap lists as JSON presets.

Two formats are understood:

* Patchwork's own: ``{"patchwork_preset": 1, "swaps": [...]}`` (the same swap
  fields as config.json; a full config.json imports too).
* Fleasion's: ``{"replacement_rules": [...]}``, where each rule has ``name``,
  ``replace_ids``, ``enabled`` and a ``mode`` of ``"id"`` (``with_id``) or
  ``"cdn"`` (``cdn_url``). Other Fleasion modes are reported as skipped.
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


def _fleasion_rule(rule: dict, name: str, result: ImportResult) -> None:
    mode = str(rule.get("mode", "")).lower()
    try:
        targets = [int(str(t).strip()) for t in _ids(rule.get("replace_ids"))]
        enabled = bool(rule.get("enabled", True))
        if mode == "id":
            with_id = str(rule.get("with_id") or "").strip()
            if with_id in ("", "0", "-1", "None"):
                # Fleasion's "remove": an ID rule with nothing to replace it with.
                swap = Swap(name, targets, KIND_REMOVE, "", enabled)
            else:
                swap = Swap(name, targets, KIND_ASSET, with_id, enabled)
        elif mode == "cdn":
            swap = Swap(name, targets, KIND_URL, str(rule.get("cdn_url", "")).strip(), enabled)
        elif mode == "local":
            path = str(rule.get("local_path", "")).strip()
            swap = Swap(name, targets, KIND_FILE, path, enabled)
            if not Path(path).is_file():
                result.missing_files.append(f"{name}: {path}")
        else:
            result.skipped.append(f'{name}: mode "{mode or "?"}" isn\'t supported yet')
            return
    except (ValueError, TypeError) as exc:
        result.skipped.append(f"{name}: {exc}")
        return
    result.swaps.append(swap)


def _fleasion_rules(rules: list, result: ImportResult, prefix: str = "", depth: int = 0) -> None:
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            result.skipped.append(f"{prefix}Rule {index + 1}: not a rule")
            continue
        name = prefix + _rule_name(rule, index)
        if rule.get("type") == "group" or "children" in rule:
            children = rule.get("children")
            if isinstance(children, list) and depth < 10:
                # Groups are folders of rules; their names prefix the rules inside.
                _fleasion_rules(children, result, f"{name} / ", depth + 1)
            else:
                result.skipped.append(f"{name}: empty or unreadable group")
            continue
        _fleasion_rule(rule, name, result)


def parse_fleasion(data: dict) -> ImportResult:
    result = ImportResult(FORMAT_FLEASION)
    rules = data.get("replacement_rules")
    if not isinstance(rules, list):
        raise ValueError("replacement_rules should be a list")
    _fleasion_rules(rules, result)
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
    if not isinstance(data, dict):
        raise ValueError("This file isn't a swap preset.")
    if "replacement_rules" in data:
        return parse_fleasion(data)
    if "swaps" in data:
        return parse_patchwork(data, base_dir)
    raise ValueError("This file isn't a Patchwork or Fleasion preset (no swaps or replacement_rules).")


def load_preset(path: Path) -> ImportResult:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"This file isn't valid JSON (line {exc.lineno}): {exc.msg}") from exc
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
