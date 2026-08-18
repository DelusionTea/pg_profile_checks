"""Cutoff tree and copyable JAVA_TOOL_OPTIONS gate for JVM checks."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
import re

import yaml

from jvmcheck.models import AnalysisResult, Finding, Recommendation
from jvmcheck.thresholds import load_thresholds

DEFAULT_PLAYBOOK = (
    Path(__file__).resolve().parents[2] / "knowledge" / "jvm_diagnostic_tree.yaml"
)
DEFAULT_FLAG_MATRIX = (
    Path(__file__).resolve().parents[2] / "knowledge" / "jvm_flag_matrix.yaml"
)
SAFE_MAX_RAM_PERCENT = 70.0
EXTRA_FLAG_PURPOSE_RU = {
    "-Xmx": "Жёсткий потолок Java heap.",
    "-Xms": "Стартовый размер Java heap.",
}

UNKNOWN = frozenset({None, "", "unknown"})
OOM_KINDS = frozenset({"oomkilled", "java_oome"})
PAUSE_RULE_PREFIXES = ("gc.long_pause", "gc.long_pause_p95", "gc.long_pause_p99")


@dataclass
class TreeAnswers:
    pods_per_shoulder: int | None = None
    restart_kind: str | None = None
    memory_cause_closed: str | None = None
    heap_growing: str | None = None
    heap_growth_percent: float | None = None
    heap_growth_hours: float | None = None
    growth_of: str | None = None
    gc_ran_in_window: str | None = None
    heap_used_before_gc_mib: int | None = None
    heap_used_after_gc_mib: int | None = None
    oldgen_returned_after_gc: str | None = None
    current_usage_percent: float | None = None
    cpu_throttled: str | None = None
    cpu_pct_limits_shoulder_1: float | None = None
    cpu_pct_limits_shoulder_2: float | None = None
    gc_pause_p95_ms: float | None = None
    user_latency_grew: str | None = None
    user_latency_p95_ms: float | None = None
    pauses_coincide_throttle: str | None = None
    post_gc_floor_rising: str | None = None
    gc_cpu_spike_sla: str | None = None


@dataclass
class TreeEvaluation:
    copyable_allowed: bool
    pause_copyable_allowed: bool
    ha_exceeded: bool
    ha_sum_pct: float | None
    gc_analysis_required: bool
    heap_branch_open: bool
    ask_user_latency: bool
    ask_cpu_pct: bool
    ask_memory_closed: bool
    ask_pauses_coincide_throttle: bool
    ask_post_gc_floor: bool = False
    ask_gc_cpu_spike: bool = False
    hours_to_oom: float | None = None
    hours_to_sla: float | None = None
    sla_not_critical: bool = False
    pauses_coincide_cpu: bool = False
    post_gc_floor_rising: bool = False
    gc_cpu_spike_sla: bool = False
    long_gc_no_symptoms: bool = False
    extreme_gc_pause: bool = False
    growth_plan_needed: bool = False
    growth_rate_pct_per_hour: float | None = None
    gc_retained_ratio: float | None = None
    heap_churn_not_leak: bool = False
    non_heap_accumulation: bool = False
    accumulation_kind: str | None = None
    await_first_gc: bool = False
    steep_growth_pending_gc: bool = False
    low_start_usage: bool = False
    repeat_window_hours: float | None = None
    block_reasons: list[str] = field(default_factory=list)
    platform_findings: list[Finding] = field(default_factory=list)
    evidence_lines: list[str] = field(default_factory=list)
    recheck_lines: list[str] = field(default_factory=list)
    dialogue_rows: list[tuple[str, str]] = field(default_factory=list)
    playbook: dict[str, Any] = field(default_factory=dict)


@dataclass
class MemoryHeapResize:
    limit_was: int | None = None
    limit_to: int | None = None
    request_was: int | None = None
    request_to: int | None = None
    heap_mode: str = "unknown"
    max_ram_pct_was: float | None = None
    max_ram_pct_to: float | None = None
    xmx_was_mib: int | None = None
    xmx_to_mib: int | None = None
    resized: bool = False


def load_playbook(path: Path | None = None) -> dict[str, Any]:
    raw = yaml.safe_load((path or DEFAULT_PLAYBOOK).read_text(encoding="utf-8")) or {}
    thresholds = load_thresholds("normal")
    return {
        "shoulders": int(raw.get("shoulders") or 2),
        "ha_cpu_sum_pct_limit": float(raw.get("ha_cpu_sum_pct_limit") or 80),
        "gc_pause_p95_ms": float(thresholds.gc_pause_p95_ms),
        "memory_sla_percent": float(raw.get("memory_sla_percent") or 80),
        "memory_sla_not_critical_days": float(raw.get("memory_sla_not_critical_days") or 30),
        "memory_plateau_example_percent": float(raw.get("memory_plateau_example_percent") or 75),
        "gc_pause_extreme_ms": float(raw.get("gc_pause_extreme_ms") or 16000),
        "gc_pending_steep_percent": float(raw.get("gc_pending_steep_percent") or 10),
        "gc_pending_steep_hours": float(raw.get("gc_pending_steep_hours") or 12),
        "gc_pending_low_usage_percent": float(raw.get("gc_pending_low_usage_percent") or 40),
        "questions": list(raw.get("questions") or []),
    }


def load_flag_purposes(path: Path | None = None) -> dict[str, str]:
    raw = yaml.safe_load((path or DEFAULT_FLAG_MATRIX).read_text(encoding="utf-8")) or {}
    out = dict(EXTRA_FLAG_PURPOSE_RU)
    for item in raw.get("flags") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        purpose = str(item.get("purpose_ru") or "").strip()
        if name and purpose:
            out[name] = purpose
    return out


def flag_purpose(flag_or_key: str, purposes: dict[str, str] | None = None) -> str:
    key = str(flag_or_key).split("=", 1)[0].strip()
    table = purposes if purposes is not None else load_flag_purposes()
    return table.get(key) or "JVM-флаг. Смотрите описание в документации JDK."


def propose_memory_heap_resize(
    *,
    limit_mib: int | None,
    request_mib: int | None,
    java_tool_options: list[str] | None,
    need_resize: bool,
) -> MemoryHeapResize:
    flags = [str(item) for item in (java_tool_options or [])]
    has_pct = any("MaxRAMPercentage" in flag for flag in flags)
    xmx_was = _parse_xmx_mib(flags)
    has_xmx = xmx_was is not None
    if has_pct and has_xmx:
        mode = "both"
    elif has_pct:
        mode = "percent"
    elif has_xmx:
        mode = "xmx"
    else:
        mode = "unknown"
    pct_was = _read_flag_number(flags, "-XX:MaxRAMPercentage")
    if mode == "percent" and pct_was is not None and limit_mib:
        xmx_was = int(limit_mib * pct_was / 100.0)

    resize = MemoryHeapResize(
        limit_was=limit_mib,
        limit_to=limit_mib,
        request_was=request_mib,
        request_to=request_mib,
        heap_mode=mode,
        max_ram_pct_was=pct_was,
        max_ram_pct_to=pct_was,
        xmx_was_mib=xmx_was,
        xmx_to_mib=xmx_was,
        resized=False,
    )
    if not need_resize or not limit_mib or limit_mib <= 0:
        return resize

    delta = max(int(limit_mib * 0.15), 256)
    limit_to = limit_mib + delta
    request_to = request_mib
    if request_mib is not None:
        request_to = min(limit_to, request_mib + delta)
    pct_to = pct_was
    if pct_was is not None and pct_was > SAFE_MAX_RAM_PERCENT:
        pct_to = SAFE_MAX_RAM_PERCENT
    xmx_to = xmx_was
    if mode == "percent" and pct_to is not None:
        xmx_to = int(limit_to * pct_to / 100.0)
    elif mode == "xmx" and xmx_was is not None:
        xmx_to = int(xmx_was * limit_to / limit_mib)
    elif mode == "both" and pct_to is not None:
        xmx_to = int(limit_to * pct_to / 100.0)
    resize.limit_to = limit_to
    resize.request_to = request_to
    resize.max_ram_pct_to = pct_to
    resize.xmx_to_mib = xmx_to
    resize.resized = True
    return resize


def _unknown(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in UNKNOWN or value.strip().lower() == "unknown"
    return value in UNKNOWN


def _norm(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    return text or None


def evaluate_jvm_diagnostic_tree(
    answers: TreeAnswers,
    playbook: dict[str, Any] | None = None,
) -> TreeEvaluation:
    book = playbook or load_playbook()
    ha_limit = float(book["ha_cpu_sum_pct_limit"])
    p95_limit = float(book["gc_pause_p95_ms"])
    sla_percent = float(book["memory_sla_percent"])
    sla_days = float(book["memory_sla_not_critical_days"])
    extreme_ms = float(book["gc_pause_extreme_ms"])
    restart = _norm(answers.restart_kind)
    throttle = _norm(answers.cpu_throttled)
    memory_closed = _norm(answers.memory_cause_closed)
    heap_growing = _norm(answers.heap_growing)
    user_latency = _norm(answers.user_latency_grew)

    heap_branch_open = restart in OOM_KINDS or heap_growing == "yes"
    ask_memory_closed = restart in OOM_KINDS
    ask_cpu_pct = throttle == "yes"
    gc_analysis_required = (
        answers.gc_pause_p95_ms is not None and float(answers.gc_pause_p95_ms) > p95_limit
    )
    ask_user_latency = gc_analysis_required
    ask_coincide = throttle == "yes" and gc_analysis_required
    extreme_gc = (
        answers.gc_pause_p95_ms is not None and float(answers.gc_pause_p95_ms) >= extreme_ms
    )
    long_gc_no_symptoms = gc_analysis_required and user_latency == "no"
    rising_floor = _norm(answers.post_gc_floor_rising) == "yes"
    gc_cpu_spike = _norm(answers.gc_cpu_spike_sla) == "yes"
    ask_post_gc_floor = heap_branch_open and _norm(answers.gc_ran_in_window) == "yes"
    ask_gc_cpu_spike = gc_analysis_required or ask_post_gc_floor

    ha_sum: float | None = None
    ha_exceeded = False
    if (
        answers.cpu_pct_limits_shoulder_1 is not None
        and answers.cpu_pct_limits_shoulder_2 is not None
    ):
        ha_sum = float(answers.cpu_pct_limits_shoulder_1) + float(
            answers.cpu_pct_limits_shoulder_2
        )
        ha_exceeded = ha_sum > ha_limit

    block_reasons: list[str] = []
    findings: list[Finding] = []

    if _unknown(restart):
        block_reasons.append(
            "Причина рестарта не указана. Копировать JAVA_TOOL_OPTIONS нельзя."
        )
        findings.append(
            Finding(
                code="tree.restart_unknown",
                severity="warning",
                message="Причина рестарта не указана.",
                details={"source": "diagnostic_tree"},
            )
        )
    elif restart == "evicted":
        block_reasons.append(
            "Контейнер вытеснен (Evicted): нехватка места на ноде или квоты namespace. "
            "Проверьте disk-pressure и квоту. G1 и `-Xmx` это не исправят."
        )
        findings.append(
            Finding(
                code="platform.evicted",
                severity="critical",
                message="Контейнер Evicted: это давление ноды или квоты, не тюнинг GC.",
                details={"source": "diagnostic_tree"},
            )
        )
    elif restart in OOM_KINDS and memory_closed != "yes":
        block_reasons.append(
            "Был OOMKilled или Java OutOfMemoryError. Пока причина по памяти не закрыта, флаги не копируем."
        )

    if _unknown(throttle):
        block_reasons.append(
            "Неизвестно, есть ли CPU throttle. Копировать JAVA_TOOL_OPTIONS нельзя."
        )
    elif throttle == "yes":
        if (
            answers.cpu_pct_limits_shoulder_1 is None
            or answers.cpu_pct_limits_shoulder_2 is None
        ):
            block_reasons.append(
                "Есть CPU throttle, но нет CPU % of limits с обоих плеч. Критичность не снижаем, флаги не копируем."
            )
        elif ha_exceeded:
            block_reasons.append(
                "Нет запаса на отказ плеча: сумма CPU % of limits с двух плеч "
                f"{ha_sum:.0f} > {ha_limit:.0f}. Это ёмкость платформы, не G1."
            )
            findings.append(
                Finding(
                    code="platform.ha_cpu_headroom",
                    severity="critical",
                    message=(
                        "Нет запаса на отказ плеча: сумма CPU % of limits этой АС "
                        f"с двух плеч {ha_sum:.0f} > {ha_limit:.0f}."
                    ),
                    threshold=f"shoulder_cpu_pct_sum > {ha_limit:.0f}",
                    evidence={
                        "shoulder_1": str(answers.cpu_pct_limits_shoulder_1),
                        "shoulder_2": str(answers.cpu_pct_limits_shoulder_2),
                    },
                    details={"source": "diagnostic_tree"},
                )
            )

    growth = _investigate_growth(
        answers, sla_percent=sla_percent, sla_days=sla_days, playbook=book
    )
    sla_not_critical = bool(growth["sla_not_critical"])
    coincide_cpu = (
        throttle == "yes"
        and gc_analysis_required
        and _norm(answers.pauses_coincide_throttle) == "yes"
    )
    if growth["non_heap"] and not sla_not_critical:
        block_reasons.append(
            "RSS растёт, а GC отрабатывает нормально. G1 это не лечит: нужен memory limit контейнера."
        )
        findings.append(
            Finding(
                code="platform.non_heap_accumulation",
                severity="critical",
                message=(
                    "RSS контейнера растёт, при этом после GC heap снизился. "
                    "GC работает. Увеличьте memory limit; G1 не копируйте."
                ),
                details={"source": "diagnostic_tree"},
            )
        )
    elif growth["non_heap"] and sla_not_critical:
        findings.append(
            Finding(
                code="platform.memory_growth_below_sla",
                severity="info",
                message=(
                    f"Память растёт, но до SLA {sla_percent:.0f}% больше {sla_days:.0f} сут — не критично."
                ),
                details={"source": "diagnostic_tree"},
            )
        )
    elif growth["mixed"]:
        block_reasons.append(
            "Растут и Java heap, и RSS контейнера. Сначала снимите heap dump (что держит heap) "
            "и отдельно посмотрите native RSS. Пока не ясно, что растёт, JVM-флаги не копируйте."
        )
    if rising_floor:
        findings.append(
            Finding(
                code="heap.rising_gc_floor",
                severity="warning",
                message=(
                    "Очистка heap есть, но минимум после каждой следующей GC выше. "
                    "Живые объекты копятся — это не плато."
                ),
                details={"source": "diagnostic_tree"},
            )
        )
    if gc_cpu_spike:
        findings.append(
            Finding(
                code="platform.gc_cpu_spike_sla",
                severity="warning",
                message=(
                    "GC очищает большой кусок heap, и всплеск CPU на этой сборке нарушает SLA."
                ),
                details={"source": "diagnostic_tree"},
            )
        )
    if (
        heap_growing == "yes"
        and growth["hours_to_oom"] is not None
        and float(growth["hours_to_oom"]) <= 24
        and not (growth["churn"] and _norm(answers.growth_of) == "heap")
    ):
        block_reasons.append(
            f"При текущей скорости контейнер упрётся в memory limit примерно через {_fmt_hours(growth['hours_to_oom'])} "
            "и будет OOMKilled. Поднимите limit до этого срока; настройка паузы GC рост не остановит."
        )

    if answers.gc_pause_p95_ms is None:
        block_reasons.append("Нет GC pause p95. Копировать JAVA_TOOL_OPTIONS нельзя.")

    copyable = not block_reasons
    pause_copyable = copyable and gc_analysis_required and user_latency == "yes"
    if coincide_cpu:
        pause_copyable = False
        findings.append(
            Finding(
                code="platform.cpu_throttle_gc_coincide",
                severity="warning",
                message=(
                    "Долгие паузы GC совпадают с CPU throttle: нехватка CPU, не настройка G1."
                ),
                details={"source": "diagnostic_tree"},
            )
        )
    if gc_cpu_spike:
        pause_copyable = False
    if gc_analysis_required and user_latency not in {"yes", "no"}:
        block_reasons.append(
            "Паузы GC выше порога, но не ясно, росли ли времена отклика. Строку для пауз не копируем."
        )

    evidence = _evidence_lines(
        answers, ha_sum, ha_limit, p95_limit, gc_analysis_required, growth
    )
    recheck = list(growth["recheck"])
    if throttle == "yes" and ha_sum is not None and not ha_exceeded:
        recheck.append(
            "Липкая сессия может неравномерно делить плечи. При сумме CPU % of limits ≤ "
            f"{ha_limit:.0f}% это не повод чинить баланс кластеров."
        )
    if copyable and not recheck:
        if pause_copyable:
            recheck.append("После выкатки снимите те же метрики в том же окне.")
        else:
            recheck.append("Снимите те же метрики в том же окне нагрузки и сравните с этим отчётом.")

    return TreeEvaluation(
        copyable_allowed=copyable,
        pause_copyable_allowed=pause_copyable,
        ha_exceeded=ha_exceeded,
        ha_sum_pct=ha_sum,
        gc_analysis_required=gc_analysis_required,
        heap_branch_open=heap_branch_open,
        ask_user_latency=ask_user_latency,
        ask_cpu_pct=ask_cpu_pct,
        ask_memory_closed=ask_memory_closed,
        ask_pauses_coincide_throttle=ask_coincide,
        ask_post_gc_floor=ask_post_gc_floor,
        ask_gc_cpu_spike=ask_gc_cpu_spike,
        hours_to_oom=growth["hours_to_oom"],
        hours_to_sla=growth["hours_to_sla"],
        sla_not_critical=sla_not_critical,
        pauses_coincide_cpu=coincide_cpu,
        post_gc_floor_rising=rising_floor,
        gc_cpu_spike_sla=gc_cpu_spike,
        long_gc_no_symptoms=long_gc_no_symptoms,
        extreme_gc_pause=extreme_gc,
        growth_plan_needed=heap_growing == "yes",
        growth_rate_pct_per_hour=growth["rate"],
        gc_retained_ratio=growth["retained_ratio"],
        heap_churn_not_leak=growth["churn"],
        non_heap_accumulation=growth["non_heap"],
        accumulation_kind=growth["space"],
        await_first_gc=growth["await_first_gc"],
        steep_growth_pending_gc=growth["steep_pending_gc"],
        low_start_usage=growth["low_start_usage"],
        repeat_window_hours=growth["repeat_window_hours"],
        block_reasons=block_reasons,
        platform_findings=findings,
        evidence_lines=evidence,
        recheck_lines=recheck,
        dialogue_rows=_dialogue_rows(answers, book),
        playbook=book,
    )


def apply_tree_gates(analysis: AnalysisResult, evaluation: TreeEvaluation) -> AnalysisResult:
    existing = {f.code for f in analysis.findings}
    for finding in evaluation.platform_findings:
        if finding.code not in existing:
            analysis.findings.append(finding)
            existing.add(finding.code)

    if evaluation.non_heap_accumulation and not evaluation.sla_not_critical:
        analysis.recommendations = [
            rec
            for rec in analysis.recommendations
            if rec.title != "No critical JVM changes required"
        ]
        if not any(rec.title == "Увеличьте memory limit контейнера" for rec in analysis.recommendations):
            analysis.recommendations.insert(0, _rss_growth_memory_recommendation())

    gated: list[Recommendation] = []
    for rec in analysis.recommendations:
        rec = replace(rec, suggested_java_tool_options=list(rec.suggested_java_tool_options))
        flags = list(rec.suggested_java_tool_options)
        if not evaluation.copyable_allowed:
            rec.suggested_java_tool_options = []
            if flags:
                rec.notes = list(rec.notes) + [f"кандидат: {flag}" for flag in flags]
        elif not evaluation.pause_copyable_allowed and _is_pause_rec(rec):
            rec.suggested_java_tool_options = []
            if flags:
                rec.notes = list(rec.notes) + [f"кандидат: {flag}" for flag in flags]
        gated.append(rec)
    analysis.recommendations = gated
    return analysis


def _rss_growth_memory_recommendation() -> Recommendation:
    return Recommendation(
        title="Увеличьте memory limit контейнера",
        rationale=(
            "RSS растёт, а GC отрабатывает нормально: после сборки heap снизился. "
            "Нехватка ёмкости контейнера, не настройка G1."
        ),
        suggested_java_tool_options=[],
        confidence="high",
        evidence_score=80,
        risk_score=35,
        expected_gain="ниже memory usage % и запас до OOMKilled",
        verification_window="то же окно нагрузки после выкатки limit",
        rollback_plan=["Вернуть прежний memory limit, если на ноде появятся Evicted."],
        rule_ids=["platform.non_heap_accumulation"],
        notes=[
            "G1 не копировать: сборщик уже работает.",
            "Цифры memory request, memory limit и расчётного `-Xmx` — в таблице «Ресурсы и JVM настройки».",
        ],
        requires_platform_escalation=True,
    )


def format_tree_wiki(
    evaluation: TreeEvaluation,
    answers: TreeAnswers,
    analysis: AnalysisResult | None = None,
    java_tool_options: list[str] | None = None,
    resize: MemoryHeapResize | None = None,
) -> str:
    lines: list[str] = []
    allowed = bool(evaluation.pause_copyable_allowed)
    lines.append("h2. Выкатывать JAVA_TOOL_OPTIONS")
    lines.append("*Да*" if allowed else "*Нет*")
    lines.append("")
    lines.append("h2. Что сделать сейчас")
    for item in _action_lines(evaluation, resize=resize):
        lines.append(f"# {item}")
    lines.append("")
    lines.append("h3. Почему так")
    for item in evaluation.evidence_lines:
        lines.append(f"* {item}")
    lines.append("")
    lines.append("h3. Что перепроверить")
    recheck = list(evaluation.recheck_lines)
    if evaluation.non_heap_accumulation and not evaluation.sla_not_critical:
        if resize and resize.resized:
            recheck.append(
                "После выкатки memory request, memory limit и `-Xmx` должны совпасть "
                "с таблицей «Ресурсы и JVM настройки». Memory usage % в том же окне должен снизиться."
            )
        else:
            recheck.extend(_heap_flag_recheck(java_tool_options, resize=resize))
    if not recheck:
        recheck = [
            "Снова снимите те же метрики в том же окне нагрузки и сравните с этим отчётом."
            if not allowed
            else "После выкатки снова снимите те же метрики в том же окне нагрузки и сравните с этим отчётом."
        ]
    for item in recheck:
        lines.append(f"* {item}")
    lines.append("")
    if evaluation.growth_plan_needed:
        lines.append("h3. План: SLA, OOM или плато")
        for item in _growth_plan_lines(evaluation):
            lines.append(f"* {item}")
        lines.append("")
    if evaluation.long_gc_no_symptoms or evaluation.extreme_gc_pause:
        lines.append("h3. Дополнительный анализ длинных пауз GC")
        for item in _long_gc_extra_lines(evaluation):
            lines.append(f"* {item}")
        lines.append("")
    lines.append("h3. Ответы по дереву")
    lines.append("|| Вопрос || Ответ ||")
    for question, value in evaluation.dialogue_rows:
        lines.append(f"| {question} | {value} |")
    lines.append("")

    candidates = _candidate_rows(analysis)
    if candidates and not allowed:
        lines.append("{expand:Кандидаты флагов (не копировать)}")
        lines.append("|| Флаг || Было || Стало || Что делает || Что снять ||")
        current_flags = _flag_value_map(java_tool_options)
        purposes = load_flag_purposes()
        for flag, _why, drop in candidates:
            key, new_val = _split_flag_pair(flag)
            was = (
                _display_flag_value(current_flags[key])
                if key in current_flags
                else "не задан"
            )
            to = _display_flag_value(new_val) if new_val is not None else "включить"
            purpose = flag_purpose(key, purposes)
            lines.append(f"| {flag} | {was} | {to} | {purpose} | {drop} |")
        lines.append("{expand}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _action_lines(
    evaluation: TreeEvaluation,
    resize: MemoryHeapResize | None = None,
) -> list[str]:
    if evaluation.ha_exceeded:
        return [
            "Нет запаса на отказ плеча: сумма CPU % of limits выше лимита. "
            "Добавьте CPU limit или поды — это ёмкость платформы, не G1 и не `-Xmx`."
        ]
    if evaluation.pauses_coincide_cpu:
        return [
            "Долгие паузы GC совпадают с CPU throttle: контейнеру не хватает CPU. "
            "Поднимите CPU limit. MaxGCPauseMillis и G1 это не исправят."
        ]
    if evaluation.gc_cpu_spike_sla:
        return [
            "GC очищает большой кусок heap, и всплеск CPU на этой сборке нарушает SLA. "
            "Поднимите CPU limit или выясните, почему между циклами копится такой объём. "
            "`-XX:MaxGCPauseMillis` всплеск CPU не уберёт. `-Xmx` не поднимайте: больший heap сделает очистку ещё тяжелее."
        ]
    if evaluation.post_gc_floor_rising:
        return [
            "Очистка heap есть, но каждая следующая GC заканчивается на большем минимуме — "
            "живые объекты копятся, проблема всё ещё есть. "
            "Снимите histogram или heap dump и отдайте топ классов разработке. "
            "`-XX:MaxGCPauseMillis` этот рост не остановит."
        ]
    if evaluation.non_heap_accumulation:
        if evaluation.sla_not_critical:
            sla_hours = evaluation.hours_to_sla
            sla_txt = _fmt_hours(sla_hours) if sla_hours is not None else "больше 30 сут"
            return [
                f"Память растёт медленно: до SLA 80% примерно {sla_txt} — больше 30 дней, не критично. "
                "Limit из-за этого роста не поднимайте. Наблюдайте memory usage %."
            ]
        lines: list[str] = []
        if resize and resize.resized and resize.limit_was and resize.limit_to:
            req_was = _fmt_mib(resize.request_was)
            req_to = _fmt_mib(resize.request_to)
            lim_was = _fmt_mib(resize.limit_was)
            lim_to = _fmt_mib(resize.limit_to)
            lines.append(
                f"Увеличьте memory request с {req_was} до {req_to} и memory limit "
                f"с {lim_was} до {lim_to}. GC отрабатывает нормально: после сборки heap снизился."
            )
        else:
            lines.append(
                "Увеличьте memory limit контейнера. RSS растёт, а GC отрабатывает нормально: после сборки heap снизился."
            )
        if evaluation.hours_to_oom is not None and evaluation.hours_to_oom <= 24:
            lines.append(
                f"При этой скорости до OOMKilled примерно {_fmt_hours(evaluation.hours_to_oom)} — limit лучше поднять раньше."
            )
        lines.extend(_heap_flag_recheck(None, resize=resize) if resize and resize.resized else [
            "Как задан heap — `-Xmx` в MiB или `-XX:MaxRAMPercentage`. "
            "Если процент, хип вырастет вместе с limit; если целое `-Xmx`, его нужно поднять вместе с limit."
        ])
        lines.append("G1 не копируйте: сборщик уже работает.")
        return lines
    if evaluation.hours_to_oom is not None and evaluation.hours_to_oom <= 24:
        hours = _fmt_hours(evaluation.hours_to_oom)
        if resize and resize.resized and resize.limit_was and resize.limit_to:
            return [
                f"При текущей скорости контейнер упрётся в memory limit примерно через {hours} "
                "и будет OOMKilled. "
                f"Поднимите memory request с {_fmt_mib(resize.request_was)} до {_fmt_mib(resize.request_to)} "
                f"и memory limit с {_fmt_mib(resize.limit_was)} до {_fmt_mib(resize.limit_to)}. "
                "Настройка паузы GC этот рост не остановит."
            ]
        return [
            f"При текущей скорости контейнер упрётся в memory limit примерно через {hours} "
            "и будет OOMKilled. Поднимите memory limit до этого срока. "
            "Настройка паузы GC этот рост не остановит."
        ]
    if evaluation.heap_churn_not_leak:
        return [
            "После GC heap снизился — это не утечка, свободная память в heap уже есть. "
            "`-Xmx` не поднимайте."
        ]
    if (
        evaluation.gc_retained_ratio is not None
        and evaluation.gc_retained_ratio >= 0.9
        and not evaluation.non_heap_accumulation
    ):
        return [
            "После GC heap почти не снизился: в heap остаются живые объекты. "
            "Снимите histogram или heap dump и отдайте топ классов разработке. "
            "`-XX:MaxGCPauseMillis` этот рост не остановит."
        ]
    if not evaluation.copyable_allowed:
        if evaluation.block_reasons:
            return list(evaluation.block_reasons)
        return [
            "Дозаполните неизвестные ответы дерева и запустите анализ снова. "
            "JVM-флаги пока не копируйте."
        ]
    if evaluation.long_gc_no_symptoms:
        return [
            "Паузы GC длинные, но времён отклика пользователя в том же окне нет. "
            "G1 и `-XX:MaxGCPauseMillis` не копируйте: без роста отклика это ещё не доказанная проблема.",
            "Снимите GC log и safepoint log. Смотрите причину паузы (Allocation Failure, Full GC, System.gc) "
            "и тип (Young, Mixed, Full), не только p95.",
            "Критично, если в том же окне растут времена отклика, падает liveness или heartbeat, "
            "есть CPU throttle, всплеск CPU на очистке бьёт по SLA, "
            "или stop-the-world на минуты на поде, который обслуживает пользователей.",
        ]
    if evaluation.gc_analysis_required and not evaluation.pause_copyable_allowed:
        return [
            "Паузы GC выше порога, но не ясно, росли ли времена отклика пользователя. "
            "Строку JAVA_TOOL_OPTIONS для пауз не копируйте, пока это не подтверждено."
        ]
    if evaluation.pause_copyable_allowed:
        return [
            "Скопируйте блок jvm-config ниже. После выкатки снова снимите GC p95 в том же окне — "
            "он должен стать ниже порога 250 мс."
        ]
    if evaluation.steep_growth_pending_gc:
        window = (
            _fmt_hours(evaluation.repeat_window_hours)
            if evaluation.repeat_window_hours
            else "то же число часов"
        )
        lines = [
            "GC за окно не отработал: рост до первой сборки ещё не утечка. Limit и G1 не трогайте.",
        ]
        if evaluation.low_start_usage:
            lines.append(
                "Текущий memory usage ещё низкий — limit из-за этого наклона не поднимайте, нужна вторая точка."
            )
        lines.append(
            f"Наклон крутой. Снимите тот же график ещё через {window} без рестарта пода: "
            "скорость упала (прогрев или кэш) или держится? Появились ли GC, throttle, рост отклика?"
        )
        return lines
    if evaluation.growth_plan_needed:
        return [
            "JAVA_TOOL_OPTIONS из-за этого роста не копируйте. "
            "Смотрите план ниже: упрётся в SLA 80%, будет OOMKilled, или выйдет на плато. "
            "Смена настроек перезапустит под и оборвёт текущий ретест."
        ]
    return [
        "JAVA_TOOL_OPTIONS копировать не нужно: паузы не выше порога 250 мс."
    ]


def _heap_flag_recheck(
    java_tool_options: list[str] | None,
    resize: MemoryHeapResize | None = None,
) -> list[str]:
    if resize is not None and (resize.resized or resize.heap_mode != "unknown"):
        return _resize_number_lines(resize)
    flags = [str(item) for item in (java_tool_options or [])]
    has_pct = any("MaxRAMPercentage" in flag for flag in flags)
    has_xmx = any(flag.strip().startswith("-Xmx") for flag in flags)
    if has_pct and has_xmx:
        return [
            "В JAVA_TOOL_OPTIONS заданы и `-Xmx`, и `-XX:MaxRAMPercentage`. Оставьте один способ — иначе размер heap непредсказуем."
        ]
    if has_pct:
        return [
            "Heap задан `-XX:MaxRAMPercentage`: при увеличении memory limit хип вырастет сам. "
            "Проверьте, что процент не слишком высокий — в контейнере должен остаться запас помимо heap. "
            "В отчёте должны быть конкретные request, limit и расчётный `-Xmx` — их нет, потому что в resources не найден memory limit."
        ]
    if has_xmx:
        return [
            "Heap задан `-Xmx` в MiB: вместе с memory limit поднимите `-Xmx`, иначе добавленная память в Java не попадёт."
        ]
    return [
        "В JAVA_TOOL_OPTIONS контейнера посмотрите, как задан heap: `-Xmx` в MiB или `-XX:MaxRAMPercentage`.",
        "Если процент — хип вырастет вместе с limit; проверьте, что процент не съедает весь контейнер.",
        "Если `-Xmx` целое — поднимите его вместе с limit.",
    ]


def _resize_number_lines(resize: MemoryHeapResize) -> list[str]:
    req_was = _fmt_mib(resize.request_was)
    req_to = _fmt_mib(resize.request_to)
    lim_was = _fmt_mib(resize.limit_was)
    lim_to = _fmt_mib(resize.limit_to)
    xmx_was = _fmt_mib(resize.xmx_was_mib)
    xmx_to = _fmt_mib(resize.xmx_to_mib)
    if resize.heap_mode == "percent" and resize.max_ram_pct_was is not None:
        pct_was = f"{resize.max_ram_pct_was:g}"
        pct_to = f"{(resize.max_ram_pct_to if resize.max_ram_pct_to is not None else resize.max_ram_pct_was):g}"
        lines = [
            f"Heap задан `-XX:MaxRAMPercentage={pct_was}`. Сейчас memory request {req_was}, "
            f"memory limit {lim_was}, расчётный `-Xmx` {xmx_was}."
        ]
        if resize.resized:
            if (
                resize.max_ram_pct_to is not None
                and resize.max_ram_pct_was is not None
                and resize.max_ram_pct_to < resize.max_ram_pct_was
            ):
                lines.append(
                    f"Процент {pct_was} высокий: снизьте `-XX:MaxRAMPercentage` до {pct_to}, "
                    "чтобы в контейнере остался запас помимо heap."
                )
            lines.append(
                f"Выставить: memory request {req_to}, memory limit {lim_to}, "
                f"`-XX:MaxRAMPercentage={pct_to}` (расчётный `-Xmx` {xmx_to})."
            )
        else:
            lines.append(
                "Проверьте, что процент не слишком высокий — в контейнере должен остаться запас помимо heap."
            )
        return lines
    if resize.heap_mode == "xmx" and resize.xmx_was_mib is not None:
        lines = [
            f"Heap задан `-Xmx{resize.xmx_was_mib}m`. Сейчас memory request {req_was}, memory limit {lim_was}."
        ]
        if resize.resized:
            lines.append(
                f"Выставить: memory request {req_to}, memory limit {lim_to}, `-Xmx{resize.xmx_to_mib}m`."
            )
        else:
            lines.append(
                "Если поднимаете memory limit, вместе с ним поднимите `-Xmx`, иначе добавленная память в Java не попадёт."
            )
        return lines
    if resize.heap_mode == "both":
        lines = [
            "Заданы и `-Xmx`, и `-XX:MaxRAMPercentage` — оставьте один способ.",
        ]
        if resize.resized:
            lines.append(
                f"Ёмкость контейнера всё равно поднять: request {req_was} → {req_to}, limit {lim_was} → {lim_to}."
            )
        return lines
    if resize.resized:
        return [
            f"Выставить: memory request {req_was} → {req_to}, memory limit {lim_was} → {lim_to}."
        ]
    return []


def answers_from_mapping(data: dict[str, Any] | None) -> TreeAnswers:
    raw = data or {}
    return TreeAnswers(
        pods_per_shoulder=_opt_int(raw.get("pods_per_shoulder")),
        restart_kind=_opt_str(raw.get("restart_kind")),
        memory_cause_closed=_opt_str(raw.get("memory_cause_closed")),
        heap_growing=_opt_str(raw.get("heap_growing")),
        heap_growth_percent=_opt_float(raw.get("heap_growth_percent")),
        heap_growth_hours=_opt_float(raw.get("heap_growth_hours")),
        growth_of=_opt_str(raw.get("growth_of")),
        gc_ran_in_window=_opt_str(raw.get("gc_ran_in_window")),
        heap_used_before_gc_mib=_opt_int(raw.get("heap_used_before_gc_mib")),
        heap_used_after_gc_mib=_opt_int(raw.get("heap_used_after_gc_mib")),
        oldgen_returned_after_gc=_opt_str(raw.get("oldgen_returned_after_gc")),
        current_usage_percent=_opt_float(raw.get("current_usage_percent")),
        cpu_throttled=_opt_str(raw.get("cpu_throttled")),
        cpu_pct_limits_shoulder_1=_opt_float(raw.get("cpu_pct_limits_shoulder_1")),
        cpu_pct_limits_shoulder_2=_opt_float(raw.get("cpu_pct_limits_shoulder_2")),
        gc_pause_p95_ms=_opt_float(raw.get("gc_pause_p95_ms")),
        user_latency_grew=_opt_str(raw.get("user_latency_grew")),
        user_latency_p95_ms=_opt_float(raw.get("user_latency_p95_ms")),
        pauses_coincide_throttle=_opt_str(raw.get("pauses_coincide_throttle")),
        post_gc_floor_rising=_opt_str(raw.get("post_gc_floor_rising")),
        gc_cpu_spike_sla=_opt_str(raw.get("gc_cpu_spike_sla")),
    )


def _is_pause_rec(rec: Recommendation) -> bool:
    rules = tuple(rec.rule_ids or ())
    return any(rule.startswith("gc.long_pause") for rule in rules)


def _candidate_rows(analysis: AnalysisResult | None) -> list[tuple[str, str, str]]:
    if analysis is None:
        return []
    rows: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for rec in analysis.recommendations:
        why = rec.rationale or rec.title
        for note in rec.notes:
            if not str(note).startswith("кандидат: "):
                continue
            flag = str(note).split("кандидат: ", 1)[1].strip()
            if flag in seen:
                continue
            seen.add(flag)
            drop = flag.split("=", 1)[0] if "=" in flag else flag
            rows.append((flag, why, drop))
        for flag in rec.suggested_java_tool_options:
            text = str(flag).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            drop = text.split("=", 1)[0] if "=" in text else text
            rows.append((text, why, drop))
    return rows


def _steep_growth_pending_gc(answers: TreeAnswers, playbook: dict[str, Any]) -> bool:
    if _norm(answers.heap_growing) != "yes" or _norm(answers.gc_ran_in_window) != "no":
        return False
    pct = answers.heap_growth_percent
    hours = answers.heap_growth_hours
    if pct is None or hours is None or float(hours) <= 0:
        return False
    steep_pct = float(playbook.get("gc_pending_steep_percent") or 10)
    steep_hours = float(playbook.get("gc_pending_steep_hours") or 12)
    if steep_hours <= 0:
        return False
    return float(pct) / float(hours) + 1e-12 >= steep_pct / steep_hours


def _investigate_growth(
    answers: TreeAnswers,
    *,
    sla_percent: float = 80.0,
    sla_days: float = 30.0,
    playbook: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rate: float | None = None
    hours_to_oom: float | None = None
    hours_to_sla: float | None = None
    if (
        answers.heap_growth_percent is not None
        and answers.heap_growth_hours is not None
        and float(answers.heap_growth_hours) > 0
    ):
        rate = float(answers.heap_growth_percent) / float(answers.heap_growth_hours)
        current = answers.current_usage_percent
        if current is not None and rate > 0:
            remaining_oom = 100.0 - float(current)
            hours_to_oom = 0.0 if remaining_oom <= 0 else remaining_oom / rate
            if float(current) >= sla_percent:
                hours_to_sla = 0.0
            else:
                hours_to_sla = (sla_percent - float(current)) / rate
        elif current is not None and rate <= 0:
            hours_to_oom = None
            hours_to_sla = None

    sla_horizon_hours = sla_days * 24.0
    sla_not_critical = hours_to_sla is not None and hours_to_sla > sla_horizon_hours

    retained: float | None = None
    churn = False
    before = answers.heap_used_before_gc_mib
    after = answers.heap_used_after_gc_mib
    if before is not None and after is not None and int(before) > 0:
        retained = float(after) / float(before)
        churn = retained <= 0.7
    rising_floor = _norm(answers.post_gc_floor_rising) == "yes"
    if rising_floor:
        churn = False

    space = _infer_accumulation_space(answers, churn=churn, retained=retained)
    if rising_floor:
        space = "heap"
    recheck: list[str] = []
    if _norm(answers.heap_growing) == "yes":
        if answers.heap_growth_percent is None or answers.heap_growth_hours is None:
            recheck.append(
                "Укажите, на сколько процентов вырос выбранный показатель и за сколько часов — иначе время до OOM не считаем."
            )
        elif hours_to_oom is None and answers.current_usage_percent is None and space != "non_heap":
            recheck.append(
                "Для оценки времени до OOM нужен текущий процент того же показателя (OldGen или memory usage контейнера)."
            )
        if _norm(answers.gc_ran_in_window) == "yes" and (before is None or after is None):
            recheck.append("Укажите heap used сразу до и сразу после GC, целыми MiB.")
        if _norm(answers.gc_ran_in_window) == "no":
            recheck.append(
                "За окно GC не отработал: рост до первой сборки ещё не утечка. Нужны heap used до и после GC."
            )
            if _steep_growth_pending_gc(answers, playbook or {}):
                pct = answers.heap_growth_percent
                hours = answers.heap_growth_hours
                recheck.append(
                    f"{pct:g}% за {_fmt_hours(hours)} — крутой наклон, даже если текущий usage ещё низкий. "
                    "Limit и G1 не трогайте до второй точки."
                )
                recheck.append(
                    f"Снимите тот же график ещё через {_fmt_hours(hours)} без рестарта пода. "
                    "Скорость упала (прогрев или кэш) или держится? "
                    "Если GC за это время отработал — вставьте heap used до и после сборки. "
                    "Смотрите, не появились ли throttle и рост отклика."
                )
        live_objects = (
            rising_floor
            or _norm(answers.oldgen_returned_after_gc) == "no"
            or (retained is not None and retained >= 0.9 and space != "non_heap")
        )
        if live_objects:
            if rising_floor:
                where = "Минимум heap после GC растёт от цикла к циклу"
            elif _norm(answers.oldgen_returned_after_gc) == "no":
                where = "OldGen после GC не снизился"
            else:
                where = "Heap после GC почти не снизился"
            recheck.extend(_live_objects_followup(where))
        if space == "non_heap":
            if sla_not_critical and hours_to_sla is not None:
                recheck.append(
                    f"До memory usage {sla_percent:.0f}% (SLA) примерно {_fmt_hours(hours_to_sla)} "
                    f"— больше {sla_days:.0f} сут, не критично. Limit из-за этого роста не поднимайте."
                )
            elif hours_to_oom is not None:
                recheck.append(
                    f"При этой скорости до OOMKilled примерно {_fmt_hours(hours_to_oom)}. "
                    "Поднимите memory limit до этого срока."
                )
            elif (
                answers.heap_growth_percent is not None
                and answers.heap_growth_hours is not None
                and answers.current_usage_percent is None
            ):
                recheck.append(
                    "Вставьте текущий memory usage % контейнера — посчитаем, через сколько часов можно упереться в OOMKilled."
                )
            if not sla_not_critical:
                recheck.append(
                    "После увеличения limit в том же окне нагрузки memory usage % должен снизиться, рестартов быть не должно."
                )
                recheck.append(
                    "Heap used сразу после GC по-прежнему должен быть заметно ниже, чем до GC. "
                    "Если перестанет снижаться — это уже живые объекты в heap, не нехватка limit."
                )
        elif churn and not live_objects:
            recheck.append(
                "После сборки heap снизился — GC отработал нормально, это не утечка."
            )
    book = playbook or {}
    await_first_gc = _norm(answers.heap_growing) == "yes" and _norm(answers.gc_ran_in_window) == "no"
    steep_pending = await_first_gc and _steep_growth_pending_gc(answers, book)
    low_usage_cut = float(book.get("gc_pending_low_usage_percent") or 40)
    current = answers.current_usage_percent
    low_start = current is not None and float(current) <= low_usage_cut
    return {
        "space": space,
        "non_heap": space == "non_heap",
        "mixed": space == "mixed",
        "rate": rate,
        "hours_to_oom": hours_to_oom,
        "hours_to_sla": hours_to_sla,
        "sla_not_critical": sla_not_critical,
        "sla_percent": sla_percent,
        "retained_ratio": retained,
        "churn": churn,
        "recheck": recheck,
        "await_first_gc": await_first_gc,
        "steep_pending_gc": steep_pending,
        "low_start_usage": bool(steep_pending and low_start),
        "repeat_window_hours": float(answers.heap_growth_hours) if steep_pending and answers.heap_growth_hours else None,
    }


def _live_objects_followup(where: str) -> list[str]:
    return [
        f"{where}: в heap остаются живые объекты. Настройка паузы GC их не удалит.",
        "Снимите histogram (`jcmd <pid> GC.class_histogram`) или heap dump. "
        "Смотрите топ классов по объёму памяти — какие объекты занимают heap.",
        "Снимите второй histogram в том же окне нагрузки и сравните, какие классы выросли. "
        "Этот список отдайте разработке: уменьшить кэш или найти утечку. "
        "Пока идёт разбор, поднимите memory limit, чтобы не получить OOMKilled.",
    ]


def _growth_plan_lines(evaluation: TreeEvaluation) -> list[str]:
    book = evaluation.playbook or {}
    sla = float(book.get("memory_sla_percent") or 80)
    plateau = float(book.get("memory_plateau_example_percent") or 75)
    days = float(book.get("memory_sla_not_critical_days") or 30)
    return [
        "Скорость считайте после выхода на стабильную динамику, не со старта пода. "
        "Прогрев и заполнение кэша в этот расчёт не входят.",
        f"Три исхода: упрётся в SLA {sla:.0f}%, будет OOMKilled / упрётся в limit, "
        f"или выйдет на плато ниже SLA (часто около {plateau:.0f}%). "
        f"Если до SLA {sla:.0f}% больше {days:.0f} дней — сейчас не критично.",
        "Плато vs утечка: несколько циклов GC без рестарта. "
        "Если минимум после каждой следующей очистки выше — это не плато, проблема всё ещё есть.",
        "Смена JAVA_TOOL_OPTIONS, memory request или memory limit перезапустит под: "
        "память обнулится принудительно. Неизвестно, сколько ждать повторного выхода на ту же динамику — "
        "ретест роста после выкатки настроек нельзя сравнивать с этим прогоном.",
    ]


def _long_gc_extra_lines(evaluation: TreeEvaluation) -> list[str]:
    extreme_ms = float((evaluation.playbook or {}).get("gc_pause_extreme_ms") or 16000)
    extreme_s = int(extreme_ms / 1000)
    lines = [
        f"Паузы от {extreme_s} с до нескольких минут без роста времён отклика встречаются. "
        "Сами по себе не повод копировать G1.",
        "Снимите GC log и safepoint: `-Xlog:gc*,safepoint*:file=/tmp/gc.log:time,uptime,level,tags`. "
        "Смотрите причину паузы (Allocation Failure, Full GC, System.gc) и тип (Young, Mixed, Full), не только p95.",
        "Сверьте паузу с графиком запросов. Если сборка не на пути пользователя (батч, очередь) — флаги паузы не копируйте.",
        "Критично, если в том же окне растут времена отклика, падает liveness или heartbeat, "
        "есть CPU throttle, всплеск CPU на очистке бьёт по SLA, "
        "или stop-the-world на минуты на поде, который обслуживает пользователей.",
    ]
    if evaluation.extreme_gc_pause:
        lines.append(
            "p95 в секундах или минутах: уточните, это редкий Full GC или регулярный цикл. "
            "Регулярный stop-the-world на минуты на поде с пользовательским трафиком — уже инцидент, "
            "даже если графика времён отклика сейчас нет."
        )
    return lines


def _infer_accumulation_space(
    answers: TreeAnswers,
    *,
    churn: bool,
    retained: float | None,
) -> str:
    growth_of = _norm(answers.growth_of)
    oldgen_back = _norm(answers.oldgen_returned_after_gc)
    if growth_of in {"oldgen", "heap"}:
        return "heap"
    if growth_of == "container_rss":
        if oldgen_back == "yes" or churn:
            return "non_heap"
        if oldgen_back == "no" or (retained is not None and retained >= 0.9):
            return "heap"
    return "unknown"


def _evidence_lines(
    answers: TreeAnswers,
    ha_sum: float | None,
    ha_limit: float,
    p95_limit: float,
    gc_analysis_required: bool,
    growth: dict[str, Any] | None = None,
) -> list[str]:
    lines = [
        f"Плеч всегда два. Подов на одном плече: {_display_answer('pods_per_shoulder', answers.pods_per_shoulder)}.",
        f"Рестарт контейнера: {_display_answer('restart_kind', answers.restart_kind)}.",
        f"Память в тесте растёт: {_display_answer('heap_growing', answers.heap_growing)}.",
        f"CPU throttle: {_display_answer('cpu_throttled', answers.cpu_throttled)}.",
    ]
    if _norm(answers.heap_growing) == "yes":
        lines.append(
            "Скорость роста считайте после выхода на стабильную динамику, не со старта пода "
            "(прогрев и заполнение кэша в этот расчёт не входят)."
        )
    if ha_sum is not None:
        lines.append(
            f"CPU % of limits по плечам: {answers.cpu_pct_limits_shoulder_1} + "
            f"{answers.cpu_pct_limits_shoulder_2} = {ha_sum:.0f} (лимит {ha_limit:.0f})."
        )
    if answers.gc_pause_p95_ms is None:
        lines.append("GC pause p95 не указан.")
    else:
        lines.append(
            f"GC pause p95 {answers.gc_pause_p95_ms:.0f} мс, порог {p95_limit:.0f} мс"
            + ("; паузы выше порога." if gc_analysis_required else "; ниже порога.")
        )
    if gc_analysis_required:
        lines.append(
            "Времена отклика пользователя в том же окне: "
            f"{_display_answer('user_latency_grew', answers.user_latency_grew)}."
        )
        if answers.pauses_coincide_throttle and not _unknown(answers.pauses_coincide_throttle):
            lines.append(
                "Совпадение пауз GC с throttle: "
                f"{_display_answer('pauses_coincide_throttle', answers.pauses_coincide_throttle)}."
            )
        if answers.gc_cpu_spike_sla and not _unknown(answers.gc_cpu_spike_sla):
            lines.append(
                "Всплеск CPU на очистке GC vs SLA: "
                f"{_display_answer('gc_cpu_spike_sla', answers.gc_cpu_spike_sla)}."
            )
    if answers.post_gc_floor_rising and not _unknown(answers.post_gc_floor_rising):
        lines.append(
            "Минимум heap после каждой следующей GC выше предыдущего: "
            f"{_display_answer('post_gc_floor_rising', answers.post_gc_floor_rising)}."
        )
    growth = growth or {}
    if answers.heap_growth_percent is not None and answers.heap_growth_hours is not None:
        metric = _display_answer("growth_of", answers.growth_of)
        rate_txt = f", {_fmt_rate(growth.get('rate'))}" if growth.get("rate") is not None else ""
        lines.append(
            f"{metric} вырос на {answers.heap_growth_percent:g}% за "
            f"{_fmt_hours(answers.heap_growth_hours)}{rate_txt}."
        )
    if growth.get("hours_to_sla") is not None:
        sla_hours = float(growth["hours_to_sla"])
        sla_pct = float(growth.get("sla_percent") or 80)
        if growth.get("sla_not_critical"):
            lines.append(
                f"До memory usage {sla_pct:.0f}% (SLA) примерно {_fmt_hours(sla_hours)} "
                f"(~{sla_hours / 24:.1f} сут) — больше 30 дней, не критично."
            )
        elif sla_hours <= 0:
            lines.append(f"Memory usage уже на уровне SLA {sla_pct:.0f}% или выше.")
        else:
            lines.append(
                f"До memory usage {sla_pct:.0f}% (SLA) примерно {_fmt_hours(sla_hours)}."
            )
    if growth.get("steep_pending_gc"):
        window = (
            _fmt_hours(answers.heap_growth_hours)
            if answers.heap_growth_hours
            else "то же окно"
        )
        lines.append(
            "GC ещё не отработал: по одному наклону рано решать, упрёмся ли в SLA. "
            f"Нужна вторая точка через {window} без рестарта пода."
        )
    if growth.get("hours_to_oom") is not None:
        hours = float(growth["hours_to_oom"])
        ceiling = {
            "oldgen": "исчерпания OldGen (Java OOME)",
            "heap": "исчерпания Java heap (OOME)",
            "container_rss": "OOMKilled контейнера",
        }.get(_norm(answers.growth_of) or "", "исчерпания выбранного показателя")
        if hours <= 0:
            lines.append(f"Показатель уже у потолка — {ceiling} при этой динамике сейчас.")
        elif hours > 168:
            lines.append(
                f"До {ceiling} примерно {_fmt_hours(hours)} "
                f"(~{hours / 24:.0f} сут) — в ближайшие сутки не дойдём."
            )
        else:
            lines.append(f"До {ceiling} примерно {_fmt_hours(hours)}.")
    elif _norm(answers.heap_growing) == "yes" and growth.get("rate") is not None and (growth.get("rate") or 0) <= 0:
        lines.append("Скорость роста не положительная — по этим цифрам до OOM не дойдём.")
    if answers.heap_used_before_gc_mib is not None and answers.heap_used_after_gc_mib is not None:
        extra = ""
        if growth.get("retained_ratio") is not None:
            if growth.get("churn"):
                extra = " После сборки heap снизился — GC отработал нормально."
            elif float(growth["retained_ratio"]) >= 0.9:
                extra = " После сборки heap почти не снизился — объекты живые."
            else:
                extra = f" Осталось {growth['retained_ratio']:.0%} от значения до сборки."
        lines.append(
            f"Heap used до GC {answers.heap_used_before_gc_mib} MiB, после GC "
            f"{answers.heap_used_after_gc_mib} MiB.{extra}"
        )
    space = growth.get("space")
    if space == "heap":
        lines.append("По введённым цифрам рост в Java heap.")
    elif space == "non_heap":
        lines.append(
            "Рос RSS контейнера, а после GC heap снизился — живой Java heap не течёт, нехватка ёмкости контейнера."
        )
    return lines


GROWTH_DIALOGUE_KEYS = frozenset(
    {
        "growth_of",
        "heap_growth_percent",
        "heap_growth_hours",
        "gc_ran_in_window",
        "heap_used_before_gc_mib",
        "heap_used_after_gc_mib",
        "oldgen_returned_after_gc",
        "post_gc_floor_rising",
    }
)
GC_WINDOW_KEYS = frozenset(
    {
        "heap_used_before_gc_mib",
        "heap_used_after_gc_mib",
        "oldgen_returned_after_gc",
        "post_gc_floor_rising",
    }
)


def _dialogue_rows(answers: TreeAnswers, playbook: dict[str, Any]) -> list[tuple[str, str]]:
    labels = {
        item.get("id"): item.get("label") or item.get("id")
        for item in playbook.get("questions") or []
        if isinstance(item, dict)
    }
    pairs = [
        ("pods_per_shoulder", answers.pods_per_shoulder),
        ("restart_kind", answers.restart_kind),
        ("heap_growing", answers.heap_growing),
        ("growth_of", answers.growth_of),
        ("heap_growth_percent", answers.heap_growth_percent),
        ("heap_growth_hours", answers.heap_growth_hours),
        ("gc_ran_in_window", answers.gc_ran_in_window),
        ("heap_used_before_gc_mib", answers.heap_used_before_gc_mib),
        ("heap_used_after_gc_mib", answers.heap_used_after_gc_mib),
        ("oldgen_returned_after_gc", answers.oldgen_returned_after_gc),
        ("post_gc_floor_rising", answers.post_gc_floor_rising),
        ("gc_cpu_spike_sla", answers.gc_cpu_spike_sla),
        ("memory_cause_closed", answers.memory_cause_closed),
        ("cpu_throttled", answers.cpu_throttled),
        ("cpu_pct_limits_shoulder_1", answers.cpu_pct_limits_shoulder_1),
        ("cpu_pct_limits_shoulder_2", answers.cpu_pct_limits_shoulder_2),
        ("gc_pause_p95_ms", answers.gc_pause_p95_ms),
        ("user_latency_grew", answers.user_latency_grew),
        ("pauses_coincide_throttle", answers.pauses_coincide_throttle),
    ]
    growing = _norm(answers.heap_growing) == "yes"
    gc_ran = _norm(answers.gc_ran_in_window) == "yes"
    rows: list[tuple[str, str]] = []
    for key, value in pairs:
        if value is None:
            continue
        if key in GROWTH_DIALOGUE_KEYS and not growing:
            continue
        if key in GC_WINDOW_KEYS and not gc_ran:
            continue
        if _unknown(value) and key not in {"restart_kind", "cpu_throttled", "heap_growing"}:
            continue
        rows.append((str(labels.get(key) or key), _display_answer(key, value)))
    return rows


CHOICE_RU = {
    "yes": "да",
    "no": "нет",
    "unknown": "не знаю",
    "none": "не рестартовал",
    "oomkilled": "OOMKilled",
    "evicted": "Evicted",
    "java_oome": "Java OutOfMemoryError",
    "oldgen": "OldGen",
    "heap": "Heap used",
    "container_rss": "RSS / memory usage контейнера",
}


def _display_answer(key: str, value: object) -> str:
    if value is None:
        return "не знаю"
    if isinstance(value, str):
        mapped = CHOICE_RU.get(value.strip().lower())
        if mapped:
            return mapped
        return value
    if key.endswith("_percent"):
        return f"{value:g} %"
    if key.endswith("_hours"):
        return _fmt_hours(value)
    if key.endswith("_mib"):
        return f"{int(value)} MiB"
    if key.endswith("_ms"):
        return f"{value:g} мс"
    return str(value)


def _display(value: object) -> str:
    return _display_answer("", value)


def _fmt_hours(value: object) -> str:
    hours = float(value)
    if abs(hours - round(hours)) < 1e-6:
        return f"{int(round(hours))} ч"
    return f"{hours:.1f} ч"


def _fmt_rate(value: object) -> str:
    rate = float(value)
    if abs(rate - round(rate)) < 1e-6:
        return f"{int(round(rate))} %/ч"
    return f"{rate:.2f} %/ч"


_XMX_UNIT_TO_MIB = {
    "k": 1 / 1024,
    "m": 1,
    "g": 1024,
    "t": 1024 * 1024,
}
_G1_FLAG_NAMES = frozenset(
    {
        "UseG1GC",
        "MaxGCPauseMillis",
        "InitiatingHeapOccupancyPercent",
        "G1ReservePercent",
        "ParallelRefProcEnabled",
        "G1HeapWastePercent",
        "G1MixedGCCountTarget",
        "G1OldCSetRegionThresholdPercent",
    }
)


def _fmt_mib(value: int | None) -> str:
    if value is None:
        return "не задан"
    return f"{value} MiB"


def _split_flag_pair(flag: str) -> tuple[str, str | None]:
    raw = str(flag or "").strip()
    if not raw:
        return "", None
    sized = re.fullmatch(r"(-Xmx|-Xms)(.+)", raw, flags=re.IGNORECASE)
    if sized:
        return sized.group(1), sized.group(2)
    if "=" in raw:
        key, value = raw.split("=", 1)
        return key.strip(), value.strip()
    plusminus = re.fullmatch(r"(-XX:)([+-])(.+)", raw)
    if plusminus:
        key = f"{plusminus.group(1)}{plusminus.group(2)}{plusminus.group(3)}"
        return key, plusminus.group(2)
    return raw, None


def _display_flag_value(value: str | None) -> str:
    if value is None or value == "":
        return "не задан"
    if value == "+":
        return "включён"
    if value == "-":
        return "выключен"
    return value


def _flag_value_map(java_tool_options: list[str] | None) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for item in java_tool_options or []:
        key, value = _split_flag_pair(str(item))
        if key:
            out[key] = value
    return out


def _read_flag_number(flags: list[str], key: str) -> float | None:
    for flag in flags:
        name, raw = _split_flag_pair(flag)
        if name != key or raw is None:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    return None


def _parse_xmx_mib(flags: list[str]) -> int | None:
    for flag in flags:
        key, raw = _split_flag_pair(flag)
        if key.lower() != "-xmx" or not raw:
            continue
        match = re.fullmatch(r"(\d+(?:\.\d+)?)([kKmMgGtT])?", raw.strip())
        if not match:
            continue
        amount = float(match.group(1))
        unit = (match.group(2) or "").lower()
        if not unit:
            return int(amount / (1024 * 1024))
        return int(amount * _XMX_UNIT_TO_MIB[unit])
    return None


def is_g1_flag_key(key: str) -> bool:
    name = re.sub(r"^-XX:[+-]?", "", str(key).strip())
    return name in _G1_FLAG_NAMES


def _opt_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _opt_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _opt_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
