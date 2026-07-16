"""Build Confluence Wiki Markup pages from advisor reports."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from compare_settings import DiffStatus
from pgprofile_advisor import AdvisorReport
from pgprofile_compare import (
    format_delta,
    format_value_cell,
    interval_diff_hours,
)
from pgprofile_classify import split_settings_rows
from pgprofile_health import CATEGORY_LABELS, CHECKERS
from pgprofile_nt_prod import NtProdValidation
from pgprofile_stable_prod import StableFinding, StableProdAnalysis, TuningRecommendation
from pgprofile_symptoms import CauseStatus, SymptomInvestigation

CONFLUENCE_PROMPT = Path(__file__).resolve().parent / "prompts" / "analyst_confluence.md"
NT_PROD_CONFLUENCE_PROMPT = (
    Path(__file__).resolve().parent / "prompts" / "analyst_confluence_nt_prod.md"
)
STABLE_PROD_CONFLUENCE_PROMPT = (
    Path(__file__).resolve().parent / "prompts" / "analyst_confluence_stable_prod.md"
)
SYMPTOM_CONFLUENCE_PROMPT = (
    Path(__file__).resolve().parent / "prompts" / "analyst_confluence_symptom.md"
)

WAL_HIGHLIGHT = ("wal_bytes", "wal_records", "wal_buffers_full", "wal_write", "wal_sync")

_SEVERITY_RANK = {
    "critical": 0,
    "high": 1,
    "warning": 2,
    "medium": 3,
    "low": 4,
    "info": 5,
}

_SEVERITY_STATUS = {
    "critical": "{status:colour=Red|title=CRITICAL|subtle=false}",
    "warning": "{status:colour=Yellow|title=WARNING|subtle=false}",
    "info": "{status:colour=Blue|title=INFO|subtle=false}",
}

_CHANGE_SAFETY_STATUS = {
    "safe": "{status:colour=Green|title=SAFE|subtle=false}",
    "cautious": "{status:colour=Yellow|title=CAUTIOUS|subtle=false}",
    "risky": "{status:colour=Red|title=RISKY|subtle=false}",
    "restart_required": "{status:colour=Yellow|title=RESTART|subtle=false}",
}

_CHANGE_IMPACT_STATUS = {
    "low": "{status:colour=Green|title=LOW|subtle=false}",
    "medium": "{status:colour=Yellow|title=MEDIUM|subtle=false}",
    "high": "{status:colour=Red|title=HIGH|subtle=false}",
}

_CAUSE_STATUS = {
    "confirmed": "{status:colour=Red|title=CONFIRMED|subtle=false}",
    "suspected": "{status:colour=Yellow|title=SUSPECTED|subtle=false}",
    "possible": "{status:colour=Blue|title=POSSIBLE|subtle=false}",
    "unlikely": "{status:colour=Green|title=UNLIKELY|subtle=false}",
}


def _status(severity: str) -> str:
    return _SEVERITY_STATUS.get(severity.lower(), _SEVERITY_STATUS["warning"])


def _change_safety_status(safety: str) -> str:
    return _CHANGE_SAFETY_STATUS.get(safety.lower(), _CHANGE_SAFETY_STATUS["cautious"])


def _change_impact_status(impact: str) -> str:
    return _CHANGE_IMPACT_STATUS.get(impact.lower(), _CHANGE_IMPACT_STATUS["medium"])


def _cause_status(status: str) -> str:
    return _CAUSE_STATUS.get(status.lower(), _CAUSE_STATUS["possible"])


def _wiki_escape(text: str) -> str:
    """Escape wiki-special chars; neutralize [..] so Confluence won't create pages."""
    return (
        str(text)
        .replace("|", "\\|")
        .replace("[", "(")
        .replace("]", ")")
        .replace("\n", " ")
    )


def _wiki_check_status(status: str) -> str:
    key = (status or "PASS").upper()
    if key == "FAIL":
        return "{status:colour=Red|title=FAIL|subtle=false}"
    if key == "SUSPECT":
        return "{status:colour=Yellow|title=SUSPECT|subtle=false}"
    return "{status:colour=Green|title=PASS|subtle=false}"


def _wiki_internal_link(label: str, anchor: str) -> str:
    """In-page Confluence link [text|#anchor] — never a create-page target."""
    text = str(label).replace("|", "/").replace("[", "(").replace("]", ")")
    safe = re.sub(r"[^\w\-]+", "_", str(anchor), flags=re.UNICODE).strip("_") or "sec"
    return f"[{text}|#{safe}]"


def _wiki_checklist_table(
    rows: list[tuple[Any, ...]],
    *,
    heading: str = "Чеклист проверок",
) -> list[str]:
    """rows: (check_name, PASS|FAIL|SUSPECT[, anchor])."""
    lines = [
        f"h2. {heading}",
        "",
        "||Проверка||Статус||",
    ]
    for row in rows:
        name = str(row[0])
        status = str(row[1])
        anchor = str(row[2]) if len(row) >= 3 and row[2] else ""
        cell = _wiki_internal_link(name, anchor) if anchor else _wiki_escape(name)
        lines.append(f"|{cell}|{_wiki_check_status(status)}|")
    if not rows:
        lines.append("|—|{status:colour=Green|title=PASS|subtle=false}|")
    lines.append("")
    return lines


def _severity_to_check_status(severity: str) -> str:
    sev = (severity or "warning").lower()
    if sev in ("info", "low"):
        return "SUSPECT"
    return "FAIL"


def _finding_category(fid: str) -> str:
    cat = str(fid).split(".", 1)[0] if fid else ""
    if cat == "db":
        cat = "io"
    if cat not in CATEGORY_LABELS and cat not in CHECKERS:
        cat = "io" if cat else "cache"
    return cat


def _checklist_from_health_findings(
    finding_rows: list[tuple[str, str, str, str]],
) -> list[tuple[str, str, str]]:
    """Map advisor findings to per-category PASS/FAIL/SUSPECT checklist."""
    by_cat: dict[str, str] = {}
    for severity, fid, _msg, _rep in finding_rows:
        cat = _finding_category(fid)
        rank = {"FAIL": 2, "SUSPECT": 1, "PASS": 0}
        new_st = _severity_to_check_status(severity)
        prev = by_cat.get(cat, "PASS")
        if rank[new_st] > rank.get(prev, 0):
            by_cat[cat] = new_st

    rows: list[tuple[str, str, str]] = []
    for cat in CHECKERS:
        label = CATEGORY_LABELS.get(cat, cat)
        rows.append((label, by_cat.get(cat, "PASS"), f"sec_{cat}"))
    return rows


def _checklist_from_symptom_causes(causes: list[Any]) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for cause in causes:
        status_val = getattr(cause.status, "value", None) or str(cause.status)
        evidence = getattr(cause, "evidence", None) or []
        if status_val == "confirmed":
            st = "FAIL"
        elif status_val == "suspected":
            st = "SUSPECT"
        elif evidence:
            st = "SUSPECT"
        else:
            st = "PASS"
        title = getattr(cause, "title", None) or getattr(cause, "cause_id", "?")
        cause_id = getattr(cause, "cause_id", None) or title
        rows.append((str(title), st, f"sec_{cause_id}"))
    return rows


def _wiki_panel(macro: str, title: str, body_lines: list[str]) -> list[str]:
    """macro: info | warning | note"""
    safe_title = str(title).replace("|", "/").replace("{", "(").replace("}", ")")
    lines = [f"{{{macro}:title={safe_title}}}"]
    lines.extend(body_lines)
    lines.append(f"{{{macro}}}")
    lines.append("")
    return lines


def _wiki_toc() -> list[str]:
    return ["{toc:maxLevel=2}", ""]


def _wiki_expand(title: str, body_lines: list[str]) -> list[str]:
    """Confluence UI Expand macro (Wiki Markup short form)."""
    safe = (title or "Детали").replace("|", "/").replace("{", "(").replace("}", ")")
    lines = [f"{{expand:{safe}}}"]
    lines.extend(body_lines)
    if body_lines and body_lines[-1] != "":
        lines.append("")
    lines.append("{expand}")
    lines.append("")
    return lines


def _wiki_anchor(name: str) -> str:
    safe = re.sub(r"[^\w\-]+", "_", name, flags=re.UNICODE).strip("_") or "sec"
    return f"{{anchor:{safe}}}"


def _wiki_actions_section(actions: list[str], *, heading: str = "Что сделать сейчас", limit: int = 8) -> list[str]:
    lines = [f"h2. {heading}", ""]
    cleaned = [a.strip() for a in actions if a and str(a).strip()]
    if not cleaned:
        lines.append("_Нет приоритетных действий — см. сводку findings._")
        lines.append("")
        return lines
    for idx, action in enumerate(cleaned[:limit], 1):
        lines.append(f"# {_wiki_escape(action)}")
    lines.append("")
    return lines


def _wiki_findings_summary_table(
    rows: list[tuple[str, str, str, str]],
    *,
    heading: str = "Сводка findings",
    group_by_category: bool = False,
) -> list[str]:
    """rows: (severity, id, message, reports)."""
    lines = [f"h2. {heading}", ""]
    if not group_by_category:
        lines.append("||Severity||ID||Сообщение||Отчёт(ы)||")
        ordered = sorted(
            rows,
            key=lambda r: (_SEVERITY_RANK.get(str(r[0]).lower(), 9), str(r[1])),
        )
        for severity, fid, message, reports in ordered:
            lines.append(
                f"|{_status(severity)}|{_wiki_escape(fid)}|"
                f"{_wiki_escape(message)}|{_wiki_escape(reports or '—')}|"
            )
        if not ordered:
            lines.append("|{status:colour=Green|title=OK|subtle=false}|—|Находок нет|—|")
        lines.append("")
        return lines

    by_cat: dict[str, list[tuple[str, str, str, str]]] = {cat: [] for cat in CHECKERS}
    for row in rows:
        by_cat.setdefault(_finding_category(row[1]), []).append(row)

    for cat in CHECKERS:
        label = CATEGORY_LABELS.get(cat, cat)
        lines.append(_wiki_anchor(f"sec_{cat}"))
        lines.append(f"h3. {label}")
        lines.append("")
        cat_rows = sorted(
            by_cat.get(cat, []),
            key=lambda r: (_SEVERITY_RANK.get(str(r[0]).lower(), 9), str(r[1])),
        )
        if not cat_rows:
            lines.append("_Нет findings — PASS._")
            lines.append("")
            continue
        lines.append("||Severity||ID||Сообщение||Отчёт(ы)||")
        for severity, fid, message, reports in cat_rows:
            lines.append(
                f"|{_status(severity)}|{_wiki_escape(fid)}|"
                f"{_wiki_escape(message)}|{_wiki_escape(reports or '—')}|"
            )
        lines.append("")
    return lines


def _wiki_llm_footer() -> list[str]:
    return [
        "----",
        "",
        "_Ниже — вывод ИИ (Confluence Wiki Markup). Вставьте ответ gigacli или используйте merge_confluence.py._",
        "",
    ]


def _collect_actions_from_advisor(reports: list[AdvisorReport], *, limit: int = 8) -> list[str]:
    actions: list[str] = []
    seen: set[str] = set()
    ranked: list[tuple[int, str]] = []
    for report in reports:
        for item in report.advised_findings:
            sev = str((item.finding or {}).get("severity") or "warning").lower()
            rank = _SEVERITY_RANK.get(sev, 9)
            for action in (item.advice or {}).get("actions") or []:
                text = str(action).strip()
                if text and text not in seen:
                    seen.add(text)
                    ranked.append((rank, text))
    ranked.sort(key=lambda x: x[0])
    for _, text in ranked[:limit]:
        actions.append(text)
    return actions


EXPLAIN_ANALYZE_PREFIX = "EXPLAIN (ANALYZE, BUFFERS)"


def _mentions_explain(*texts: str) -> bool:
    return any("EXPLAIN" in (t or "").upper() for t in texts)


def _wrap_explain_analyze(sql: str) -> str:
    """Return copy-paste ready EXPLAIN (ANALYZE, BUFFERS) + SQL."""
    body = (sql or "").strip()
    if not body:
        return ""
    # Avoid double-wrapping if already an EXPLAIN statement.
    if body.lstrip().upper().startswith("EXPLAIN"):
        return body if body.rstrip().endswith(";") else body.rstrip() + ";"
    body = body.rstrip().rstrip(";")
    return f"{EXPLAIN_ANALYZE_PREFIX}\n{body};"


def _sql_code_wiki(title: str, explain_sql: str) -> list[str]:
    safe_title = (title or "SQL").replace("|", "/").replace("{", "(").replace("}", ")")[:100]
    # Confluence {code} body must not contain a lone {code} closer; rare in SQL.
    body = explain_sql.replace("{code}", "{code }")
    return [
        f"{{code:language=sql|title={safe_title}}}",
        body,
        "{code}",
        "",
    ]


def _full_query_from_ctx(ctx: Any, hex_id: str | None) -> str | None:
    if not hex_id or not getattr(ctx, "queries_by_id", None):
        return None
    text = ctx.queries_by_id.get(hex_id) or ctx.queries_by_id.get(str(hex_id).lstrip("0x"))
    text = (text or "").strip()
    return text or None


def _pick_top_query_rows(ctx: Any, *, metric: str, top_n: int = 3) -> list[dict[str, Any]]:
    """Pick top statement rows by metric for EXPLAIN candidates."""
    rows: list[dict[str, Any]] = []
    if metric == "sum_cpu_time":
        source = list(getattr(ctx, "top_rusage_statements", None) or [])
        source.sort(key=lambda r: float(r.get("sum_cpu_time") or 0), reverse=True)
        rows = source[:top_n]
    elif metric == "wal_bytes":
        source = list(getattr(ctx, "top_statements", None) or [])
        source.sort(key=lambda r: float(r.get("wal_bytes") or 0), reverse=True)
        rows = source[:top_n]
    elif metric == "io_time":
        source = list(getattr(ctx, "top_statements", None) or [])
        source.sort(key=lambda r: float(r.get("io_time") or 0), reverse=True)
        rows = source[:top_n]
    else:
        source = list(getattr(ctx, "top_statements", None) or [])
        source.sort(
            key=lambda r: float(r.get("total_exec_time") or r.get("total_time") or 0),
            reverse=True,
        )
        rows = source[:top_n]
    return rows


def _metric_for_explain_context(cause_ids: list[str], symptom: str | None = None) -> str:
    joined = " ".join(cause_ids) + " " + (symptom or "")
    if "wal" in joined:
        return "wal_bytes"
    if "io" in joined or "temp" in joined:
        return "io_time"
    if "cpu" in joined or "jit" in joined or "parallel" in joined:
        return "sum_cpu_time"
    return "total_exec_time"


def _collect_explain_queries_from_symptom(
    inv: SymptomInvestigation,
    *,
    top_n: int = 3,
) -> list[dict[str, str]]:
    """Queries that need EXPLAIN ANALYZE for this investigation (deduped)."""
    explain_causes = [
        c
        for c in inv.causes
        if c.status in (CauseStatus.CONFIRMED, CauseStatus.SUSPECTED, CauseStatus.POSSIBLE)
        and (
            c.evidence
            or c.status != CauseStatus.POSSIBLE
        )
        and _mentions_explain(*(c.confirm_actions or []))
    ]
    plan_wants = _mentions_explain(*inv.action_plan)
    if not explain_causes and not plan_wants and not inv.query_matches:
        return []

    cause_ids = [c.cause_id for c in explain_causes] or [inv.symptom]
    metric = _metric_for_explain_context(cause_ids, inv.symptom)
    collected: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(hex_id: str | None, sql: str | None, label: str, note: str = "") -> None:
        if not sql or not hex_id or hex_id in seen:
            return
        seen.add(hex_id)
        title = f"hex={hex_id}"
        if label:
            title = f"{label} · {title}"
        if note:
            title = f"{title} · {note}"
        collected.append(
            {
                "hexqueryid": hex_id,
                "title": title,
                "sql": sql,
                "explain_sql": _wrap_explain_analyze(sql),
            }
        )

    for match in inv.query_matches or []:
        hex_id = str(match.get("hexqueryid") or "")
        sql = None
        for snap in inv.reports:
            sql = _full_query_from_ctx(snap.ctx, hex_id)
            if sql:
                break
        add(hex_id, sql, "slow_query", str(match.get("dbname") or ""))

    for snap in inv.reports:
        for row in _pick_top_query_rows(snap.ctx, metric=metric, top_n=top_n):
            hex_id = str(row.get("hexqueryid") or "")
            sql = _full_query_from_ctx(snap.ctx, hex_id)
            note = metric
            if metric == "sum_cpu_time" and row.get("sum_cpu_time") is not None:
                note = f"sum_cpu_time={float(row['sum_cpu_time']):.1f}s"
            add(hex_id, sql, snap.label, note)

    limit = top_n * max(len(inv.reports), 1)
    return collected[: max(limit, top_n)]


def _collect_explain_queries_from_advisor(
    reports: list[AdvisorReport],
    *,
    top_n: int = 5,
) -> list[dict[str, str]]:
    """EXPLAIN candidates from health findings that recommend EXPLAIN or carry query_text."""
    collected: list[dict[str, str]] = []
    seen: set[str] = set()
    for report in reports:
        for advised in report.advised_findings:
            advice = advised.advice or {}
            actions = [str(a) for a in (advice.get("actions") or [])]
            rec_text = str(advice.get("recommendation") or "")
            finding = advised.finding or {}
            details = finding.get("details") or {}
            hex_id = str(details.get("hexqueryid") or "")
            sql = (details.get("query_text") or "").strip()
            wants = _mentions_explain(rec_text, *actions) or bool(sql and hex_id)
            if not wants or not sql or not hex_id or hex_id in seen:
                continue
            seen.add(hex_id)
            fid = str(finding.get("id") or "query")
            collected.append(
                {
                    "hexqueryid": hex_id,
                    "title": f"{fid} · hex={hex_id}",
                    "sql": sql,
                    "explain_sql": _wrap_explain_analyze(sql),
                }
            )
            if len(collected) >= top_n:
                return collected
    return collected


def _collect_explain_queries_from_stable(
    analysis: StableProdAnalysis,
    *,
    top_n: int = 3,
) -> list[dict[str, str]]:
    """Top SQL for EXPLAIN when stable-prod recommendations mention EXPLAIN."""
    wants = False
    for rec in analysis.recommendations:
        texts = [rec.title, *(rec.operational or [])]
        advice = rec.problem_advice or {}
        texts.append(str(advice.get("recommendation") or ""))
        texts.extend(str(a) for a in (advice.get("actions") or []))
        if _mentions_explain(*texts):
            wants = True
            break
    if not wants:
        return []

    collected: list[dict[str, str]] = []
    seen: set[str] = set()
    for snap in analysis.reports:
        for row in _pick_top_query_rows(snap.ctx, metric="total_exec_time", top_n=top_n):
            hex_id = str(row.get("hexqueryid") or "")
            sql = _full_query_from_ctx(snap.ctx, hex_id)
            if not sql or not hex_id or hex_id in seen:
                continue
            seen.add(hex_id)
            collected.append(
                {
                    "hexqueryid": hex_id,
                    "title": f"{snap.label} · hex={hex_id}",
                    "sql": sql,
                    "explain_sql": _wrap_explain_analyze(sql),
                }
            )
    return collected[: top_n * max(len(analysis.reports), 1)]


def _explain_analyze_wiki_section(
    queries: list[dict[str, str]],
    *,
    heading: str = "EXPLAIN (ANALYZE, BUFFERS) — скопировать в psql",
) -> list[str]:
    if not queries:
        return []
    body: list[str] = [
        "{info}Ниже готовые команды для вставки в psql/клиент. "
        "На prod осторожно: ANALYZE выполняет запрос. "
        "При необходимости замените литералы/параметры на актуальные значения.{info}",
        "",
    ]
    for item in queries:
        hex_id = item.get("hexqueryid") or ""
        preview = (item.get("title") or "SQL").replace("|", "/")[:80]
        expand_title = f"SQL · hex={hex_id}" if hex_id else preview
        body.extend(_wiki_expand(expand_title, _sql_code_wiki(preview, item["explain_sql"])))
    return [f"h2. {heading}", ""] + body


def explain_analyze_wiki_for_symptom(
    inv: SymptomInvestigation,
    *,
    heading: str | None = None,
) -> list[str]:
    """Public helper for NT multi-run / UI combined wikis."""
    return _explain_analyze_wiki_section(
        _collect_explain_queries_from_symptom(inv),
        heading=heading
        or f"EXPLAIN (ANALYZE, BUFFERS) — {inv.symptom_title}",
    )


def _page_title(reports: list[AdvisorReport]) -> str:
    for report in reports:
        if report.source_type == "health_check":
            meta = report.meta.get("report_meta", {})
            server = meta.get("server", "PostgreSQL")
            end = meta.get("report_end", "")
            date_part = str(end)[:10] if end else datetime.now().strftime("%Y-%m-%d")
            return f"pg_profile: {server} ({date_part})"
        if report.source_type == "run_comparison":
            a = report.meta.get("run_a", {}).get("run_id", "A")
            b = report.meta.get("run_b", {}).get("run_id", "B")
            return f"pg_profile: сравнение {a} vs {b}"
        if report.source_type == "settings_diff":
            a = report.meta.get("run_a", {}).get("run_id", "NT")
            b = report.meta.get("run_b", {}).get("run_id", "PROD")
            return f"pg_profile: настройки {a} vs {b}"
    return "pg_profile: отчёт анализа"


def _metadata_rows(reports: list[AdvisorReport]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = [
        ("Дата формирования", datetime.now().strftime("%Y-%m-%d %H:%M")),
        ("Инструмент", "pg_profile_checks / analyze_pgprofile.py"),
    ]
    for report in reports:
        if report.source_type == "health_check":
            meta = report.meta.get("report_meta", {})
            rows.extend(
                [
                    ("Сервер", str(meta.get("server", "?"))),
                    ("Интервал", f"{meta.get('report_start', '?')} — {meta.get('report_end', '?')}"),
                    ("Длительность", f"{meta.get('interval_hours', '?')} ч"),
                    ("Файл отчёта", str(meta.get("filename", "?"))),
                ]
            )
        elif report.source_type == "run_comparison":
            run_a = report.meta.get("run_a", {})
            run_b = report.meta.get("run_b", {})
            rows.extend(
                [
                    ("Прогон A", str(run_a.get("run_id", "?"))),
                    ("Прогон B", str(run_b.get("run_id", "?"))),
                    ("Длительность A", f"{run_a.get('interval_hours', '?')} ч"),
                    ("Длительность B", f"{run_b.get('interval_hours', '?')} ч"),
                ]
            )
            if report.meta.get("interval_mismatch"):
                rows.append(
                    (
                        "Разница интервалов",
                        f"{report.meta.get('interval_diff_hours', '?')} ч — сравнивайте /час",
                    )
                )
        elif report.source_type == "settings_diff":
            rows.extend(
                [
                    ("Среда A", str(report.meta.get("run_a", {}).get("run_id", "?"))),
                    ("Среда B", str(report.meta.get("run_b", {}).get("run_id", "?"))),
                ]
            )
    total = sum(r.summary.get("total_findings", 0) for r in reports)
    high = sum(r.summary.get("high_priority", 0) for r in reports)
    rows.append(("Всего находок", str(total)))
    rows.append(("Высокий приоритет", str(high)))
    return rows


def _findings_table(reports: list[AdvisorReport]) -> str:
    lines = [
        "h2. Сводка находок",
        "",
        "||Приоритет||Находка||ID||Сообщение||",
    ]
    for report in reports:
        for item in report.advised_findings:
            f = item.finding
            advice = item.advice
            severity = f.get("severity", "warning")
            title = _wiki_escape(str(advice.get("title", f.get("id", "?"))))
            fid = _wiki_escape(str(f.get("id", "?")))
            message = _wiki_escape(str(f.get("message", "")))
            lines.append(f"|{_status(severity)}|{title}|{fid}|{message}|")
    if len(lines) == 3:
        lines.append("|{status:colour=Green|title=OK|subtle=false}|Находок нет|—|—|")
    lines.append("")
    return "\n".join(lines)


def build_confluence_stub(
    reports: list[AdvisorReport],
    *,
    page_title: str | None = None,
) -> str:
    title = page_title or _page_title(reports)
    total = sum(r.summary.get("total_findings", 0) for r in reports)
    high = sum(r.summary.get("high_priority", 0) for r in reports)
    finding_rows: list[tuple[str, str, str, str]] = []
    for report in reports:
        meta = report.meta or {}
        rm = meta.get("report_meta") if isinstance(meta.get("report_meta"), dict) else {}
        report_label = str(
            (rm or {}).get("filename")
            or meta.get("filename")
            or meta.get("label")
            or "—"
        )
        for item in report.advised_findings:
            f = item.finding or {}
            finding_rows.append(
                (
                    str(f.get("severity") or "warning"),
                    str(f.get("id") or "?"),
                    str(f.get("message") or "")[:160],
                    report_label,
                )
            )

    checklist = _checklist_from_health_findings(finding_rows)
    fail_n = sum(1 for row in checklist if row[1] == "FAIL")
    suspect_n = sum(1 for row in checklist if row[1] == "SUSPECT")
    pass_n = sum(1 for row in checklist if row[1] == "PASS")

    verdict_macro = "warning" if fail_n or high or total else "info"
    verdict_title = "Краткий вердикт" if total else "Краткий вердикт — чисто"
    verdict_body = [
        f"Чеклист: FAIL *{fail_n}* · SUSPECT *{suspect_n}* · PASS *{pass_n}*.",
        f"Находок: *{total}* (высокий приоритет: *{high}*).",
        "Сначала чеклист и действия, затем детали findings / EXPLAIN в Expand.",
    ]
    if not total:
        verdict_body = [
            f"Чеклист: FAIL *{fail_n}* · SUSPECT *{suspect_n}* · PASS *{pass_n}*.",
            "Критических/предупреждающих находок по порогам нет.",
        ]

    lines: list[str] = [f"h1. {title}", ""]
    lines.extend(_wiki_panel(verdict_macro, verdict_title, verdict_body))
    lines.extend(_wiki_checklist_table(checklist))
    lines.extend(_wiki_toc())
    lines.extend(_wiki_actions_section(_collect_actions_from_advisor(reports)))
    lines.extend(_wiki_findings_summary_table(finding_rows, group_by_category=True))
    lines.extend(
        _wiki_expand(
            "Справочно: параметры анализа",
            ["||Параметр||Значение||"]
            + [f"|{_wiki_escape(k)}|{_wiki_escape(v)}|" for k, v in _metadata_rows(reports)]
            + [""],
        )
    )
    lines.extend(
        _explain_analyze_wiki_section(_collect_explain_queries_from_advisor(reports))
    )
    lines.extend(_wiki_llm_footer())
    return "\n".join(lines).rstrip() + "\n"


def build_confluence_llm_prompt(brief: str) -> str:
    template = CONFLUENCE_PROMPT.read_text(encoding="utf-8") if CONFLUENCE_PROMPT.exists() else ""
    return (
        f"{template}\n\n---\n\n"
        "# DATA FOR ANALYSIS\n\n"
        f"{brief}\n\n---\n\n"
        "Начни ответ сразу с `h2. Краткое резюме` (без h1 и без пояснений)."
    )


def merge_confluence_page(stub: str, llm_body: str) -> str:
    body = llm_body.strip()
    for prefix in ("```", "```wiki", "```confluence"):
        if body.startswith(prefix):
            body = body.split("\n", 1)[-1]
    if body.endswith("```"):
        body = body.rsplit("```", 1)[0]
    body = body.strip()

    marker = "_Ниже — вывод ИИ"
    if marker in stub:
        head, _ = stub.split(marker, 1)
        stub = head.rstrip() + "\n"
    return stub.rstrip() + "\n\n" + body + "\n"


def _nt_prod_metric_wiki_table(
    items: list[Any],
    col_nt: str,
    col_prod: str,
    *,
    show_per_hour: bool = True,
    max_rows: int = 25,
) -> list[str]:
    if not items:
        return ["_Нет значимых расхождений._", ""]

    lines = [
        f"||Метрика||{col_nt}||{col_prod}||Delta||",
    ]
    for diff in items[:max_rows]:
        key = _wiki_escape(diff.key)
        nt_v = _wiki_escape(format_value_cell(diff, "a", show_per_hour=show_per_hour))
        prod_v = _wiki_escape(format_value_cell(diff, "b", show_per_hour=show_per_hour))
        delta = _wiki_escape(format_delta(diff))
        lines.append(f"|{key}|{nt_v}|{prod_v}|{delta}|")
        if diff.key == "wal_bytes" and show_per_hour:
            ph_a, ph_b = diff.per_hour_a, diff.per_hour_b
            if ph_a is not None or ph_b is not None:
                nt_mb = f"{ph_a / 1_048_576:.2f} MB/h" if ph_a is not None else "-"
                prod_mb = f"{ph_b / 1_048_576:.2f} MB/h" if ph_b is not None else "-"
                lines.append(
                    f"|{_wiki_escape('  wal throughput')}|{nt_mb}|{prod_mb}| |"
                )
    if len(items) > max_rows:
        lines.append(f"|...|ещё {len(items) - max_rows} строк| | |")
    lines.append("")
    return lines


def build_nt_prod_confluence_stub(
    validation: NtProdValidation,
    *,
    page_title: str | None = None,
) -> str:
    run_nt = validation.run_nt
    run_prod = validation.run_prod
    s = validation.settings
    col_nt = run_nt.run_id
    col_prod = run_prod.run_id
    show_ph = True

    title = page_title or "pg_profile: валидация НТ vs ПРОМ"
    lines: list[str] = [f"h1. {title}", ""]

    critical, informational = split_settings_rows(s.rows)

    if not s.valid:
        lines.extend(
            [
                "{warning:title=ПРОГОН НЕВАЛИДЕН}",
                f"Defined settings GUC расходятся: *{s.critical_count}* критичных отличий.",
                "Сравнение метрик *не следует* использовать до выравнивания GUC.",
                "{warning}",
                "",
            ]
        )
    elif informational:
        lines.extend(
            [
                "{status:colour=Green|title=GUC OK|subtle=false} Конфигурация GUC совпадает",
                "",
                "{info:title=Справочно: runtime-метаданные}",
                f"Есть *{s.informational_count}* отличий runtime (pg_conf_load_time, postmaster_start, in_hot_standby) — "
                "не блокируют сравнение.",
                "{info}",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "{status:colour=Green|title=OK|subtle=false} "
                "Настройки НТ и ПРОМ совпадают (Defined settings)",
                "",
            ]
        )

    lines.extend(
        [
            "h2. Параметры сравнения",
            "",
            "||Параметр||Значение||",
            f"|Дата формирования|{datetime.now().strftime('%Y-%m-%d %H:%M')}|",
            f"|НТ отчёт|{_wiki_escape(run_nt.path.name)}|",
            f"|ПРОМ отчёт|{_wiki_escape(run_prod.path.name)}|",
            f"|НТ интервал|{run_nt.ctx.interval_hours:.1f} ч|",
            f"|ПРОМ интервал|{run_prod.ctx.interval_hours:.1f} ч|",
            f"|Порог расхождения|>= {validation.min_change_pct:g}%|",
            f"|GUC валидность|{'OK' if s.valid else 'НЕВАЛИДНО'}|",
            f"|Справочно: объём WAL/операций|{validation.info_count}|",
            f"|Предупреждения производительности|{validation.warning_count}|",
            "",
        ]
    )

    diff_h = interval_diff_hours(run_nt, run_prod)
    if diff_h > 0.01:
        lines.extend(
            [
                "{note:title=Разная длительность интервалов}",
                f"Интервалы отличаются на *{diff_h:.1f} ч*. Счётчики в таблицах показаны с нормализацией */час*.",
                "{note}",
                "",
            ]
        )

    if critical:
        lines.extend(["h2. Критичные расхождения GUC", "", "||Параметр||НТ||ПРОМ||Статус||"])
        for row in critical[:20]:
            status = row.status.value
            nt_v = _wiki_escape(row.nt_value or "—")
            prod_v = _wiki_escape(row.prod_value or "—")
            lines.append(f"|{_wiki_escape(row.name)}|{nt_v}|{prod_v}|{status}|")
        lines.append("")

    if informational:
        lines.extend(
            ["h2. Справочно: runtime / метаданные", "", "||Параметр||НТ||ПРОМ||Статус||"]
        )
        for row in informational[:20]:
            status = row.status.value
            nt_v = _wiki_escape(row.nt_value or "—")
            prod_v = _wiki_escape(row.prod_value or "—")
            lines.append(f"|{_wiki_escape(row.name)}|{nt_v}|{prod_v}|{status}|")
        lines.append("")

    grouped_info: dict[str, list[Any]] = {}
    for diff in validation.metric_diffs_info:
        grouped_info.setdefault(diff.section, []).append(diff)
    grouped_warn: dict[str, list[Any]] = {}
    for diff in validation.metric_diffs_warning:
        grouped_warn.setdefault(diff.section, []).append(diff)

    info_priority = [
        ("wal", "Справочно: WAL / объём"),
        ("dml", "Справочно: DML операции"),
        ("cluster", "Справочно: checkpoints (объём)"),
        ("tables", "Справочно: DML по таблицам"),
    ]
    for section, heading in info_priority:
        if section not in validation.sections:
            continue
        items = grouped_info.get(section, [])
        if not items:
            continue
        if section == "wal":
            wal_first = [d for d in items if d.key in WAL_HIGHLIGHT]
            wal_rest = [d for d in items if d.key not in WAL_HIGHLIGHT]
            items = wal_first + wal_rest
        lines.append(f"h2. {heading}")
        lines.append("")
        lines.extend(_nt_prod_metric_wiki_table(items, col_nt, col_prod, show_per_hour=show_ph))

    warn_priority = [
        ("wal", "WAL — предупреждения"),
        ("cluster", "Checkpoints — предупреждения"),
        ("cache", "Cache — предупреждения"),
    ]
    for section, heading in warn_priority:
        if section not in validation.sections:
            continue
        items = grouped_warn.get(section, [])
        if not items:
            continue
        lines.append(f"h2. {heading}")
        lines.append("")
        lines.extend(_nt_prod_metric_wiki_table(items, col_nt, col_prod, show_per_hour=show_ph))

    if validation.query_groups_info and "queries" in validation.sections:
        lines.extend(["h2. Справочно: SQL — объём и calls", ""])
        lines.append(f"||Запрос||Параметр||{col_nt}||{col_prod}||Delta||")
        row_count = 0
        for group in validation.query_groups_info[:12]:
            label = _wiki_escape(group.label)
            for diff in group.fields[:4]:
                if row_count >= 30:
                    break
                nt_v = _wiki_escape(format_value_cell(diff, "a", show_per_hour=show_ph))
                prod_v = _wiki_escape(format_value_cell(diff, "b", show_per_hour=show_ph))
                delta = _wiki_escape(format_delta(diff))
                lines.append(
                    f"|{label}|{_wiki_escape(diff.key)}|{nt_v}|{prod_v}|{delta}|"
                )
                row_count += 1
        lines.append("")

    if validation.query_groups_warning and "queries" in validation.sections:
        lines.extend(["h2. SQL — производительность (mean/max time)", ""])
        lines.append(f"||Запрос||Параметр||{col_nt}||{col_prod}||Delta||")
        row_count = 0
        for group in validation.query_groups_warning[:12]:
            label = _wiki_escape(group.label)
            for diff in group.fields[:4]:
                if row_count >= 20:
                    break
                nt_v = _wiki_escape(format_value_cell(diff, "a", show_per_hour=show_ph))
                prod_v = _wiki_escape(format_value_cell(diff, "b", show_per_hour=show_ph))
                delta = _wiki_escape(format_delta(diff))
                lines.append(
                    f"|{label}|{_wiki_escape(diff.key)}|{nt_v}|{prod_v}|{delta}|"
                )
                row_count += 1
        lines.append("")

    lines.extend(
        [
            "{info:title=О документе}",
            "Таблицы сформированы Python (compare_nt_prod). Краткая интерпретация — разделы ниже от ИИ.",
            "{info}",
            "",
            "----",
            "",
            "_Ниже — вывод ИИ (Confluence Wiki Markup). Вставьте ответ gigacli или merge_confluence.py._",
            "",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def build_nt_prod_confluence_prompt(brief: str) -> str:
    template = (
        NT_PROD_CONFLUENCE_PROMPT.read_text(encoding="utf-8")
        if NT_PROD_CONFLUENCE_PROMPT.exists()
        else ""
    )
    return (
        f"{template}\n\n---\n\n"
        "# DATA FOR ANALYSIS\n\n"
        f"{brief}\n\n---\n\n"
        "Начни ответ сразу с `h2. Краткое резюме` (без h1 и без пояснений)."
    )


def write_nt_prod_confluence_outputs(
    validation: NtProdValidation,
    output_dir: Path,
    *,
    page_title: str | None = None,
) -> None:
    from pgprofile_nt_prod import build_nt_prod_brief

    output_dir.mkdir(parents=True, exist_ok=True)
    brief = build_nt_prod_brief(validation)
    (output_dir / "nt_prod_brief.md").write_text(brief, encoding="utf-8")
    (output_dir / "nt_prod_confluence_stub.wiki").write_text(
        build_nt_prod_confluence_stub(validation, page_title=page_title),
        encoding="utf-8",
    )
    (output_dir / "nt_prod_confluence_prompt.txt").write_text(
        build_nt_prod_confluence_prompt(brief),
        encoding="utf-8",
    )


def _stable_prod_page_title(analysis: StableProdAnalysis, page_title: str | None) -> str:
    if page_title:
        return page_title
    servers: set[str] = set()
    for snap in analysis.reports:
        name = snap.ctx.properties.get("server_name") or snap.path.stem
        servers.add(str(name))
    n = len(analysis.reports)
    if len(servers) == 1:
        return f"pg_profile: полный анализ {n} отчётов ({next(iter(servers))})"
    return f"pg_profile: полный анализ {n} отчётов"


def _guc_current_wiki(guc_items: list[Any]) -> str:
    if not guc_items:
        return "—"
    parts: list[str] = []
    for item in guc_items[:3]:
        if isinstance(item, dict):
            guc_name = item.get("guc", "?")
            current = item.get("current_values", {})
        else:
            guc_name = getattr(item, "guc", "?")
            current = getattr(item, "current_values", {})
        if current:
            cur = ", ".join(f"{k}={v}" for k, v in list(current.items())[:4])
            parts.append(f"{guc_name}: {cur}")
        else:
            parts.append(str(guc_name))
    return _wiki_escape("; ".join(parts))


def _recommendations_summary_table(recommendations: list[TuningRecommendation]) -> list[str]:
    lines = [
        "h2. Общие проблемы (во всех отчётах) — рекомендации по GUC",
        "",
        "||Критичность||Проблема||Стабильность||Безопасность||Влияние||GUC (текущие)||Finding ID||",
    ]
    if not recommendations:
        lines.append(
            "|{status:colour=Green|title=OK|subtle=false}|Общих проблем нет|—|—|—|—|—|"
        )
        lines.append("")
        return lines

    for rec in recommendations:
        sf = rec.stable_finding
        title = _wiki_escape(rec.title)
        stability = _wiki_escape(
            f"{sf.occurrence_count}/{sf.total_reports} ({sf.stability_ratio:.0%})"
        )
        guc_cur = _guc_current_wiki(rec.guc_items)
        fid = _wiki_escape(rec.finding_rule_id)
        lines.append(
            f"|{_status(rec.problem_severity)}|{title}|{stability}|"
            f"{_change_safety_status(rec.combined_safety)}|"
            f"{_change_impact_status(rec.combined_impact)}|{guc_cur}|{fid}|"
        )
    lines.append("")
    return lines


def _ephemeral_findings_by_report_wiki(analysis: StableProdAnalysis) -> list[str]:
    """Problems not present in every report, grouped by report label."""
    if not analysis.ephemeral_findings:
        return []

    lines: list[str] = [
        "h2. Проблемы отдельных отчётов",
        "",
        "{note}Ниже — findings, которые есть не во всех загруженных отчётах "
        "(специфичны для одного или части файлов).{note}",
        "",
    ]

    # occurrence in some but not all — show once with labels
    partial = [ef for ef in analysis.ephemeral_findings if ef.occurrence_count > 1]
    single = [ef for ef in analysis.ephemeral_findings if ef.occurrence_count == 1]

    if partial:
        lines.extend(
            [
                "h3. В части отчётов (не во всех)",
                "",
                "||Severity||Finding||Отчёты||",
            ]
        )
        for ef in partial[:40]:
            labels = _wiki_escape(", ".join(ef.report_labels))
            reports = _wiki_escape(f"{ef.occurrence_count}/{ef.total_reports}")
            msg = _wiki_escape((ef.sample_messages[0][:120] if ef.sample_messages else ef.rule_id))
            lines.append(
                f"|{_status(ef.max_severity)}|{_wiki_escape(ef.rule_id)} — {msg}|"
                f"{reports} ({labels})|"
            )
        lines.append("")

    by_label: dict[str, list[StableFinding]] = {}
    for ef in single:
        for label in ef.report_labels:
            by_label.setdefault(label, []).append(ef)

    if by_label:
        lines.append("h3. Только в одном отчёте")
        lines.append("")
        for label in sorted(by_label.keys()):
            lines.append(f"h4. {_wiki_escape(label)}")
            lines.append("")
            lines.append("||Severity||Finding||Сообщение||")
            for ef in by_label[label][:30]:
                msg = _wiki_escape(
                    (ef.sample_messages[0][:160] if ef.sample_messages else "—")
                )
                lines.append(
                    f"|{_status(ef.max_severity)}|{_wiki_escape(ef.rule_id)}|{msg}|"
                )
            lines.append("")

    return lines


def _guc_details_table(recommendations: list[TuningRecommendation]) -> list[str]:
    lines = [
        "h2. Детали GUC",
        "",
        "||GUC||Направление||Безопасность||Влияние||Текущие значения||Postgres Pro||",
    ]
    row_count = 0
    for rec in recommendations:
        for guc in rec.guc_items:
            if row_count >= 40:
                break
            cur = ", ".join(f"{k}={v}" for k, v in guc.current_values.items()) or "—"
            pgpro = _wiki_escape(guc.postgres_pro.splitlines()[0][:120]) if guc.postgres_pro else "—"
            lines.append(
                f"|{_wiki_escape(guc.guc)}|{_wiki_escape(guc.direction)}|"
                f"{_change_safety_status(guc.change_safety)}|"
                f"{_change_impact_status(guc.change_impact)}|"
                f"{_wiki_escape(cur)}|{pgpro}|"
            )
            row_count += 1
    if row_count == 0:
        lines.append("|—|—|—|—|—|—|")
    lines.append("")
    return lines


def build_stable_prod_confluence_stub(
    analysis: StableProdAnalysis,
    *,
    page_title: str | None = None,
) -> str:
    title = _stable_prod_page_title(analysis, page_title)
    critical = sum(1 for r in analysis.recommendations if r.problem_severity == "critical")
    high = sum(1 for r in analysis.recommendations if r.problem_severity == "high")

    actions: list[str] = []
    for rec in analysis.recommendations:
        for op in rec.operational or []:
            if op not in actions:
                actions.append(op)
        for g in rec.guc_items or []:
            text = f"Рассмотреть {g.guc}: {g.direction}"
            if text not in actions:
                actions.append(text)
        if len(actions) >= 8:
            break

    finding_rows = [
        (
            sf.max_severity,
            sf.rule_id,
            (sf.sample_messages[0] if sf.sample_messages else sf.rule_id)[:160],
            ", ".join(sf.report_labels) if sf.report_labels else "все",
        )
        for sf in analysis.stable_findings
    ]
    checklist = _checklist_from_health_findings(finding_rows)
    fail_n = sum(1 for row in checklist if row[1] == "FAIL")
    suspect_n = sum(1 for row in checklist if row[1] == "SUSPECT")
    pass_n = sum(1 for row in checklist if row[1] == "PASS")

    verdict_macro = "warning" if critical or high or fail_n else "info"
    verdict_body = [
        f"Чеклист (общие): FAIL *{fail_n}* · SUSPECT *{suspect_n}* · PASS *{pass_n}*.",
        f"Общие findings: *{len(analysis.stable_findings)}*; tuning: *{len(analysis.recommendations)}* "
        f"(critical/high: {critical}/{high}).",
        f"Специфичные (не во всех): *{len(analysis.ephemeral_findings)}*. "
        f"Min stability: {analysis.min_stability_ratio:.0%}.",
    ]

    lines: list[str] = [f"h1. {title}", ""]
    lines.extend(_wiki_panel(verdict_macro, "Краткий вердикт", verdict_body))
    lines.extend(_wiki_checklist_table(checklist, heading="Чеклист проверок (общие findings)"))
    lines.extend(_wiki_toc())
    lines.extend(_wiki_actions_section(actions))
    lines.extend(_wiki_findings_summary_table(finding_rows, heading="Сводка общих findings"))
    lines.extend(_recommendations_summary_table(analysis.recommendations))
    guc_lines = _guc_details_table(analysis.recommendations)
    if guc_lines:
        # drop leading h2 + blank for expand body
        body = guc_lines[2:] if guc_lines and guc_lines[0].startswith("h2.") else guc_lines
        lines.extend(_wiki_expand("Детали GUC", body))
    ephemeral = _ephemeral_findings_by_report_wiki(analysis)
    if ephemeral:
        body = ephemeral[2:] if ephemeral and ephemeral[0].startswith("h2.") else ephemeral
        lines.extend(_wiki_expand("Проблемы отдельных отчётов", body))
    report_meta = [
        "||Метка||Файл||Интервал||Длительность||Findings||",
    ]
    for snap in analysis.reports:
        props = snap.ctx.properties
        interval = f"{props.get('report_start1', '?')} .. {props.get('report_end1', '?')}"
        report_meta.append(
            f"|{_wiki_escape(snap.label)}|{_wiki_escape(snap.path.name)}|"
            f"{_wiki_escape(interval)}|{snap.ctx.interval_hours:.1f} ч|{len(snap.findings)}|"
        )
    report_meta.append("")
    lines.extend(
        _wiki_expand(
            "Справочно: параметры и отчёты",
            [
                "||Параметр||Значение||",
                f"|Дата формирования|{datetime.now().strftime('%Y-%m-%d %H:%M')}|",
                f"|Отчётов|{len(analysis.reports)}|",
                f"|Общие findings|{len(analysis.stable_findings)}|",
                f"|Специфичные findings|{len(analysis.ephemeral_findings)}|",
                "",
                "h3. Отчёты",
                "",
                *report_meta,
            ],
        )
    )
    lines.extend(
        _explain_analyze_wiki_section(_collect_explain_queries_from_stable(analysis))
    )
    lines.extend(_wiki_llm_footer())
    return "\n".join(lines).rstrip() + "\n"


def build_stable_prod_confluence_prompt(brief: str) -> str:
    template = (
        STABLE_PROD_CONFLUENCE_PROMPT.read_text(encoding="utf-8")
        if STABLE_PROD_CONFLUENCE_PROMPT.exists()
        else ""
    )
    return (
        f"{template}\n\n---\n\n"
        "# DATA FOR ANALYSIS\n\n"
        f"{brief}\n\n---\n\n"
        "Начни ответ сразу с `h2. Краткое резюме` (без h1 и без пояснений)."
    )


def write_stable_prod_confluence_outputs(
    analysis: StableProdAnalysis,
    output_dir: Path,
    *,
    page_title: str | None = None,
) -> None:
    from pgprofile_stable_prod import build_stable_prod_brief

    output_dir.mkdir(parents=True, exist_ok=True)
    brief = build_stable_prod_brief(analysis)
    (output_dir / "stable_prod_brief.md").write_text(brief, encoding="utf-8")
    (output_dir / "stable_prod_confluence_stub.wiki").write_text(
        build_stable_prod_confluence_stub(analysis, page_title=page_title),
        encoding="utf-8",
    )
    (output_dir / "stable_prod_confluence_prompt.txt").write_text(
        build_stable_prod_confluence_prompt(brief),
        encoding="utf-8",
    )


def _symptom_page_title(inv: SymptomInvestigation, page_title: str | None) -> str:
    if page_title:
        return page_title
    return f"pg_profile: {inv.symptom_title}"


def build_symptom_confluence_stub(
    inv: SymptomInvestigation,
    *,
    page_title: str | None = None,
) -> str:
    title = _symptom_page_title(inv, page_title)
    confirmed = sum(1 for c in inv.causes if c.status == CauseStatus.CONFIRMED)
    suspected = sum(1 for c in inv.causes if c.status == CauseStatus.SUSPECTED)

    finding_rows = [
        (
            "critical" if c.status == CauseStatus.CONFIRMED else "warning" if c.status == CauseStatus.SUSPECTED else "info",
            c.cause_id,
            c.title,
            ", ".join(c.reports_matched) if c.reports_matched else "—",
        )
        for c in inv.causes
        if c.status in (CauseStatus.CONFIRMED, CauseStatus.SUSPECTED) or c.evidence
    ]

    checklist = _checklist_from_symptom_causes(inv.causes)
    fail_n = sum(1 for row in checklist if row[1] == "FAIL")
    suspect_n = sum(1 for row in checklist if row[1] == "SUSPECT")
    pass_n = sum(1 for row in checklist if row[1] == "PASS")

    verdict_macro = "warning" if confirmed else ("note" if suspected else "info")
    verdict_body = [
        f"Симптом: *{_wiki_escape(inv.symptom_title)}* (`{inv.symptom}`).",
        f"Чеклист гипотез: FAIL *{fail_n}* · SUSPECT *{suspect_n}* · PASS *{pass_n}*.",
        f"Confirmed / Suspected: *{confirmed}* / *{suspected}*. "
        "FAIL=confirmed, SUSPECT=suspected, PASS=possible без evidence.",
    ]

    lines: list[str] = [f"h1. {title}", ""]
    lines.extend(_wiki_panel(verdict_macro, "Краткий вердикт", verdict_body))
    lines.extend(_wiki_checklist_table(checklist, heading="Чеклист гипотез"))
    lines.extend(_wiki_toc())
    lines.extend(_wiki_actions_section(inv.action_plan[:8], heading="Что сделать сейчас (verify)"))
    lines.extend(_wiki_findings_summary_table(finding_rows, heading="Сводка гипотез"))

    hyp_lines = [
        "||Статус||Причина||ID||Отчёты||Evidence (кратко)||",
    ]
    for cause in inv.causes:
        ev = _wiki_escape("; ".join(cause.evidence[:2])) if cause.evidence else "—"
        reports = _wiki_escape(", ".join(cause.reports_matched)) if cause.reports_matched else "—"
        hyp_lines.append(
            f"|{_cause_status(cause.status.value)}|{_wiki_escape(cause.title)}|"
            f"{_wiki_escape(cause.cause_id)}|{reports}|{ev}|"
        )
    hyp_lines.append("")
    lines.extend(["h2. Гипотезы (возможные причины)", ""] + hyp_lines)

    detail_body: list[str] = []
    for cause in inv.causes[:12]:
        detail_body.append(_wiki_anchor(f"sec_{cause.cause_id}"))
        detail_body.append(f"h3. {_wiki_escape(cause.title)} ({cause.cause_id})")
        detail_body.append("")
        if cause.status == CauseStatus.POSSIBLE and not cause.evidence:
            detail_body.append("_Нет evidence в отчёте (PASS)._")
            detail_body.append("")
            continue
        if cause.evidence:
            detail_body.append("*Evidence:*")
            for ev in cause.evidence[:4]:
                detail_body.append(f"* {_wiki_escape(ev)}")
            detail_body.append("")
        if cause.confirm_actions:
            detail_body.append("*Подтвердить:*")
            for action in cause.confirm_actions[:3]:
                detail_body.append(f"# {_wiki_escape(action)}")
            detail_body.append("")
        if cause.refute_actions:
            detail_body.append("*Опровергнуть:*")
            for action in cause.refute_actions[:2]:
                detail_body.append(f"# {_wiki_escape(action)}")
            detail_body.append("")
    if detail_body:
        lines.extend(_wiki_expand("Детали: confirm / refute", detail_body))

    if inv.query_matches:
        qm = ["||hexqueryid||DB||Preview||"]
        for m in inv.query_matches[:5]:
            preview = _wiki_escape((m.get("preview") or "")[:100])
            qm.append(
                f"|{_wiki_escape(str(m.get('hexqueryid')))}|"
                f"{_wiki_escape(str(m.get('dbname', '?')))}|{preview}|"
            )
        qm.append("")
        lines.extend(_wiki_expand("Найденные запросы (slow_query)", qm))

    meta_body = [
        "||Параметр||Значение||",
        f"|Дата формирования|{datetime.now().strftime('%Y-%m-%d %H:%M')}|",
        f"|Симптом|{_wiki_escape(inv.symptom_title)} ({inv.symptom})|",
        f"|Отчётов|{len(inv.reports)}|",
        f"|Confirmed / Suspected|{confirmed} / {suspected}|",
    ]
    if inv.query_target:
        meta_body.append(f"|Целевой запрос|{_wiki_escape(inv.query_target.describe())}|")
    meta_body.extend(["", "||Метка||Файл||Интервал||"])
    for snap in inv.reports:
        props = snap.ctx.properties
        interval = f"{props.get('report_start1', '?')} .. {props.get('report_end1', '?')}"
        meta_body.append(
            f"|{_wiki_escape(snap.label)}|{_wiki_escape(snap.path.name)}|{_wiki_escape(interval)}|"
        )
    meta_body.append("")
    lines.extend(_wiki_expand("Справочно: параметры расследования", meta_body))

    lines.extend(
        _explain_analyze_wiki_section(_collect_explain_queries_from_symptom(inv))
    )
    lines.extend(_wiki_llm_footer())
    return "\n".join(lines).rstrip() + "\n"


def build_symptom_confluence_prompt(brief: str) -> str:
    template = (
        SYMPTOM_CONFLUENCE_PROMPT.read_text(encoding="utf-8")
        if SYMPTOM_CONFLUENCE_PROMPT.exists()
        else ""
    )
    return (
        f"{template}\n\n---\n\n"
        "# DATA FOR ANALYSIS\n\n"
        f"{brief}\n\n---\n\n"
        "Начни ответ сразу с `h2. Краткое резюме` (без h1 и без пояснений)."
    )


def write_symptom_confluence_outputs(
    inv: SymptomInvestigation,
    output_dir: Path,
    *,
    page_title: str | None = None,
) -> None:
    from pgprofile_symptoms import build_symptom_brief

    output_dir.mkdir(parents=True, exist_ok=True)
    brief = build_symptom_brief(inv)
    (output_dir / "symptom_brief.md").write_text(brief, encoding="utf-8")
    (output_dir / "symptom_confluence_stub.wiki").write_text(
        build_symptom_confluence_stub(inv, page_title=page_title),
        encoding="utf-8",
    )
    (output_dir / "symptom_confluence_prompt.txt").write_text(
        build_symptom_confluence_prompt(brief),
        encoding="utf-8",
    )
