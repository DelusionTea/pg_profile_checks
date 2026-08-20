"""DML etalon from one or more pg_profile reports (worst-case raw counters)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pgprofile_health import load_report_data
from pgprofile_parser import parse_report_period

DML_OPS = ("insert", "update", "delete")
_METRIC_BY_OP = {
    "insert": "n_tup_ins",
    "update": "n_tup_upd",
    "delete": "n_tup_del",
}
_TECHNICAL_SCHEMAS = frozenset({"pg_catalog", "pg_toast", "information_schema"})


@dataclass
class DmlEtalonRow:
    relname: str
    insert: int
    update: int
    delete: int
    source_insert: str | None = None
    source_update: str | None = None
    source_delete: str | None = None

    def total(self) -> int:
        return self.insert + self.update + self.delete


@dataclass
class DmlEtalonReport:
    path: Path
    filename: str
    label: str
    period: str


@dataclass
class DmlEtalon:
    reports: list[DmlEtalonReport]
    tables: list[DmlEtalonRow] = field(default_factory=list)
    page_title: str = "Эталон DML с ПРОМ"

    @property
    def report_count(self) -> int:
        return len(self.reports)

    @property
    def single_report(self) -> bool:
        return self.report_count == 1


def is_app_table(row: dict[str, Any]) -> bool:
    """Application table: not profiler/catalog, not t_repl_*."""
    dbname = str(row.get("dbname") or "")
    schema = str(row.get("schemaname") or "")
    relname = str(row.get("relname") or "").strip()
    if not relname:
        return False
    if dbname == "postgres":
        return False
    if schema in _TECHNICAL_SCHEMAS or "pgse_profile" in schema:
        return False
    if relname.startswith("t_repl"):
        return False
    return True


def _dml_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _wiki_cell(text: str) -> str:
    return (
        str(text)
        .replace("|", "&#124;")
        .replace("[", "(")
        .replace("]", ")")
        .replace("{", "(")
        .replace("}", ")")
        .replace("\n", " ")
    )


def etalon_from_table_rows(
    reports: list[tuple[str, list[dict[str, Any]]]],
) -> list[DmlEtalonRow]:
    """Max raw n_tup_ins/upd/del per relname across named report snapshots."""
    best: dict[str, dict[str, tuple[int, str]]] = {}
    for report_id, rows in reports:
        for row in rows:
            if not isinstance(row, dict) or not is_app_table(row):
                continue
            relname = str(row.get("relname") or "").strip()
            slot = best.setdefault(relname, {})
            for op, metric in _METRIC_BY_OP.items():
                value = _dml_int(row.get(metric))
                if value is None:
                    continue
                current = slot.get(op)
                if current is None or value > current[0]:
                    slot[op] = (value, report_id)

    tables: list[DmlEtalonRow] = []
    for relname, slot in best.items():
        insert = slot.get("insert")
        update = slot.get("update")
        delete = slot.get("delete")
        ins_n = insert[0] if insert else 0
        upd_n = update[0] if update else 0
        del_n = delete[0] if delete else 0
        if ins_n == 0 and upd_n == 0 and del_n == 0:
            continue
        tables.append(
            DmlEtalonRow(
                relname=relname,
                insert=ins_n,
                update=upd_n,
                delete=del_n,
                source_insert=insert[1] if insert else None,
                source_update=update[1] if update else None,
                source_delete=delete[1] if delete else None,
            )
        )
    tables.sort(key=lambda row: (-row.total(), row.relname))
    return tables


def build_dml_etalon(
    paths: list[Path],
    *,
    labels: list[str] | None = None,
    page_title: str | None = None,
) -> DmlEtalon:
    if not paths:
        raise ValueError("нужен хотя бы один отчёт pg_profile")
    label_list = list(labels or [])
    snapshots: list[tuple[str, list[dict[str, Any]]]] = []
    reports: list[DmlEtalonReport] = []
    for i, path in enumerate(paths):
        ctx = load_report_data(path)
        filename = path.name
        label = label_list[i] if i < len(label_list) and label_list[i] else filename
        period = parse_report_period(path).label()
        reports.append(
            DmlEtalonReport(path=path, filename=filename, label=label, period=period)
        )
        snapshots.append((filename, list(ctx.top_tables or [])))
    return DmlEtalon(
        reports=reports,
        tables=etalon_from_table_rows(snapshots),
        page_title=(page_title or "").strip() or "Эталон DML с ПРОМ",
    )


def dml_etalon_to_dict(etalon: DmlEtalon) -> dict[str, Any]:
    return {
        "type": "dml_etalon",
        "page_title": etalon.page_title,
        "report_count": etalon.report_count,
        "single_report": etalon.single_report,
        "reports": [
            {
                "filename": item.filename,
                "label": item.label,
                "period": item.period,
            }
            for item in etalon.reports
        ],
        "tables": [
            {
                "relname": row.relname,
                "insert": row.insert,
                "update": row.update,
                "delete": row.delete,
                "source": {
                    "insert": row.source_insert,
                    "update": row.source_update,
                    "delete": row.source_delete,
                },
            }
            for row in etalon.tables
        ],
    }


def _scope_note(etalon: DmlEtalon) -> str:
    if etalon.single_report:
        return "Эталон по одному отчёту: max по нескольким проливкам не считался."
    return (
        f"Эталон по {etalon.report_count} отчётам: "
        "по каждой операции взято максимальное сырое значение."
    )


def build_dml_etalon_wiki(etalon: DmlEtalon, *, page_title: str | None = None) -> str:
    title = (page_title or etalon.page_title).strip() or "Эталон DML с ПРОМ"
    lines = [
        f"h1. {_wiki_cell(title)}",
        "",
        f"{{info}}{_wiki_cell(_scope_note(etalon))}{{info}}",
        "",
        "||имя таблицы бд||insert||update||delete||",
    ]
    for row in etalon.tables:
        lines.append(
            f"|{_wiki_cell(row.relname)}|{row.insert}|{row.update}|{row.delete}|"
        )
    lines.append("")
    return "\n".join(lines)


def build_dml_etalon_brief(etalon: DmlEtalon) -> str:
    lines = [
        f"# {etalon.page_title}",
        "",
        _scope_note(etalon),
        "",
        "| имя таблицы бд | insert | update | delete |",
        "| --- | ---: | ---: | ---: |",
    ]
    for row in etalon.tables:
        lines.append(f"| {row.relname} | {row.insert} | {row.update} | {row.delete} |")
    lines.append("")
    return "\n".join(lines)


def write_dml_etalon_outputs(
    etalon: DmlEtalon,
    output_dir: Path,
    *,
    page_title: str | None = None,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    wiki = build_dml_etalon_wiki(etalon, page_title=page_title)
    brief = build_dml_etalon_brief(etalon)
    payload = dml_etalon_to_dict(etalon)
    (output_dir / "dml_etalon.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "dml_etalon_confluence_stub.wiki").write_text(wiki, encoding="utf-8")
    (output_dir / "dml_etalon_brief.md").write_text(brief, encoding="utf-8")
    (output_dir / "brief.md").write_text(brief, encoding="utf-8")
