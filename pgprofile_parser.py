"""Parse pg_profile HTML reports and extract PostgreSQL settings."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

DATA_MARKER = "const data="


class PgProfileParseError(Exception):
    """Raised when an HTML report cannot be parsed."""


def extract_data_json(html: str, *, source: str = "<html>") -> dict:
    """Extract and parse the ``const data={...}`` JSON object from report HTML."""
    start = html.find(DATA_MARKER)
    if start < 0:
        raise PgProfileParseError(f"{source}: marker '{DATA_MARKER}' not found")

    idx = start + len(DATA_MARKER)
    if idx >= len(html) or html[idx] != "{":
        raise PgProfileParseError(f"{source}: JSON object not found after '{DATA_MARKER}'")

    depth = 0
    in_str = False
    escaped = False

    for i in range(idx, len(html)):
        char = html[i]
        if in_str:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_str = False
            continue

        if char == '"':
            in_str = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                payload = html[idx : i + 1]
                try:
                    return json.loads(payload)
                except json.JSONDecodeError as exc:
                    raise PgProfileParseError(
                        f"{source}: invalid JSON in report data: {exc}"
                    ) from exc

    raise PgProfileParseError(f"{source}: unterminated JSON object in report data")


def normalize_value(value: object) -> str:
    """Normalize a GUC value for display and comparison."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value).strip()


def normalize_setting_name(name: object) -> str:
    """Normalize a setting name for stable matching across reports."""
    if name is None:
        return ""
    text = str(name).replace("\u00a0", " ").strip().lower()
    return " ".join(text.split())


def load_settings(
    html_path: Path,
    *,
    defined_only: bool = True,
) -> dict[str, str]:
    """Load settings from a pg_profile HTML report as ``{name: reset_val}``."""
    html = html_path.read_text(encoding="utf-8")
    data = extract_data_json(html, source=str(html_path))

    try:
        rows = data["datasets"]["settings"]
    except KeyError as exc:
        raise PgProfileParseError(
            f"{html_path}: dataset 'settings' not found in report data"
        ) from exc

    if defined_only:
        rows = [row for row in rows if row.get("defined_val")]

    settings: dict[str, str] = {}
    for row in rows:
        raw_name = row.get("name")
        name = normalize_setting_name(raw_name)
        if not name:
            continue
        if name in settings:
            print(
                f"warning: duplicate setting '{raw_name}' in {html_path}, using last value",
                file=sys.stderr,
            )
        settings[name] = normalize_value(row.get("reset_val"))

    return settings


def parse_report_meta(html_path: Path) -> dict[str, str]:
    """Extract report metadata from filename and settings dataset."""
    meta = {
        "path": str(html_path),
        "filename": html_path.name,
        "server": "",
        "from": "",
        "to": "",
        "version": "",
    }

    match = re.search(
        r"pgprofile_srv=(?P<server>[^_]+(?:_[^_]+)*)_from=(?P<from>[^_]+(?:_[^_]+)*)_to=(?P<to>[^.]+)",
        html_path.name,
    )
    if match:
        meta["server"] = match.group("server").replace("_", ".")
        meta["from"] = match.group("from").replace("_", " ")
        meta["to"] = match.group("to").replace("_", " ")

    html = html_path.read_text(encoding="utf-8")
    data = extract_data_json(html, source=str(html_path))
    for row in data.get("datasets", {}).get("settings", []):
        if row.get("h_ord") is not None:
            meta["version"] = normalize_value(row.get("reset_val"))
            break

    return meta


def load_report(html_path: Path) -> dict:
    """Load full report data object from pg_profile HTML."""
    html = html_path.read_text(encoding="utf-8")
    return extract_data_json(html, source=str(html_path))


def load_all_settings(data: dict) -> dict[str, str]:
    """Build a name -> value map from all settings rows in report data."""
    settings: dict[str, str] = {}
    for row in data.get("datasets", {}).get("settings", []):
        name = normalize_setting_name(row.get("name"))
        if name:
            settings[name] = normalize_value(row.get("reset_val"))
    return settings
