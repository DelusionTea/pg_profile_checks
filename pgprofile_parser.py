"""Parse pg_profile HTML reports and extract PostgreSQL settings."""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

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


@dataclass(frozen=True)
class ReportPeriod:
    """Time window a pg_profile report covers.

    ``source`` says where the window came from: ``report`` (properties dataset,
    authoritative), ``filename`` (``from=..._to=...`` in the name) or
    ``unknown``.
    """

    start: datetime | None = None
    end: datetime | None = None
    source: str = "unknown"

    @property
    def known(self) -> bool:
        return self.start is not None or self.end is not None

    def sort_timestamp(self) -> float | None:
        """Comparable timestamp for chronological ordering, None if unknown."""
        anchor = self.start or self.end
        if anchor is None:
            return None
        if anchor.tzinfo is None:
            return anchor.replace(tzinfo=timezone.utc).timestamp()
        return anchor.timestamp()

    def order_key(self, tiebreak: str = "") -> tuple[int, float, str]:
        """Chronological sort key; reports without a known period sort last."""
        timestamp = self.sort_timestamp()
        if timestamp is None:
            return (1, 0.0, tiebreak)
        return (0, timestamp, tiebreak)

    def label(self) -> str:
        if not self.known:
            return "период неизвестен"
        start = self.start.strftime("%Y-%m-%d %H:%M") if self.start else "?"
        end = self.end.strftime("%Y-%m-%d %H:%M") if self.end else "?"
        return f"{start} .. {end}"


@dataclass(frozen=True)
class ReportOrder:
    """Reports (and their labels) rearranged into chronological order."""

    paths: list[Path]
    labels: list[str]
    periods: list[ReportPeriod]
    changed: bool
    undated: list[Path]

    def note(self) -> str:
        parts = []
        for index, path in enumerate(self.paths):
            label = self.labels[index] if index < len(self.labels) else ""
            period = self.periods[index]
            suffix = f" ({label})" if label else ""
            parts.append(f"{period.label()}{suffix}")
        note = "порядок отчётов выставлен по дате отчёта: " + " → ".join(parts)
        if self.undated:
            names = ", ".join(p.name for p in self.undated)
            note += f"; без даты (в конец): {names}"
        return note


def _report_datetime(text: object, unix_seconds: object) -> datetime | None:
    """Parse a pg_profile timestamp, keeping the report's own UTC offset."""
    if isinstance(text, str) and text.strip():
        candidate = text.strip()
        if " " in candidate and "T" not in candidate:
            candidate = candidate.replace(" ", "T", 1)
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            pass
    try:
        if unix_seconds is not None:
            return datetime.fromtimestamp(float(unix_seconds), tz=timezone.utc)
    except (TypeError, ValueError):
        pass
    return None


def _filename_datetime(stamp: str) -> datetime | None:
    """Parse ``2026_08_11_16_30`` from a report filename (Y m d H M, tail optional)."""
    parts = [int(p) for p in stamp.split("_") if p.isdigit()]
    if len(parts) < 3:
        return None
    padded = (parts + [0, 0])[:5]
    try:
        return datetime(*padded)  # type: ignore[arg-type]
    except ValueError:
        return None


_PERIOD_CACHE: dict[tuple[str, int, int], ReportPeriod] = {}


def parse_report_period(html_path: Path) -> ReportPeriod:
    """Read the interval a report covers, preferring the report data over the filename.

    Upload flows rename files, so the filename is only a fallback. Results are
    cached per (path, mtime, size) because reports are megabyte-sized.
    """
    html_path = Path(html_path)
    try:
        stat = html_path.stat()
        cache_key = (str(html_path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        cache_key = None
    if cache_key is not None and cache_key in _PERIOD_CACHE:
        return _PERIOD_CACHE[cache_key]

    period = ReportPeriod()
    try:
        data = extract_data_json(
            html_path.read_text(encoding="utf-8"), source=str(html_path)
        )
        properties = (data.get("datasets", {}).get("properties") or [{}])[0]
        start = _report_datetime(
            properties.get("report_start1"), properties.get("report_start1_ut")
        )
        end = _report_datetime(properties.get("report_end1"), properties.get("report_end1_ut"))
        if start or end:
            period = ReportPeriod(start=start, end=end, source="report")
    except (OSError, PgProfileParseError, IndexError, AttributeError):
        period = ReportPeriod()

    if not period.known:
        match = re.search(
            r"from=(?P<from>[\d_]+)_to=(?P<to>[\d_]+)",
            html_path.name,
        )
        if match:
            start = _filename_datetime(match.group("from"))
            end = _filename_datetime(match.group("to"))
            if start or end:
                period = ReportPeriod(start=start, end=end, source="filename")

    if cache_key is not None:
        if len(_PERIOD_CACHE) > 512:  # long-lived UI server: keep the cache bounded
            _PERIOD_CACHE.clear()
        _PERIOD_CACHE[cache_key] = period
    return period


def sort_reports_by_date(
    paths: Sequence[Path],
    labels: Sequence[str] | None = None,
) -> ReportOrder:
    """Order reports chronologically; reports without a period go last.

    Labels travel with their own report so that ``прогон 1`` stays attached to
    the earliest run regardless of upload or argument order.
    """
    items = [Path(p) for p in paths]
    label_list = [str(x) for x in (labels or [])]
    periods = [parse_report_period(p) for p in items]

    indexed = list(range(len(items)))
    ordered = sorted(indexed, key=lambda i: periods[i].order_key(items[i].name))
    return ReportOrder(
        paths=[items[i] for i in ordered],
        labels=[label_list[i] for i in ordered if i < len(label_list)],
        periods=[periods[i] for i in ordered],
        changed=ordered != indexed,
        undated=[items[i] for i in ordered if not periods[i].known],
    )


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
