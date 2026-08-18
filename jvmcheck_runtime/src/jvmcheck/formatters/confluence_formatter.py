from __future__ import annotations

from jvmcheck.diagnostic_tree import (
    MemoryHeapResize,
    _display_flag_value,
    _flag_value_map,
    _fmt_mib,
    _split_flag_pair,
    flag_purpose,
    is_g1_flag_key,
    load_flag_purposes,
)
from jvmcheck.models import AnalysisResult, ContainerResources, MultiRunAnalysis, RuntimeContext, RuntimeMetrics


def format_analysis_for_confluence(
    analysis: AnalysisResult,
    container: ContainerResources,
    runtime_metrics: RuntimeMetrics,
    runtime_context: RuntimeContext,
    system_name: str | None = None,
    trend: MultiRunAnalysis | None = None,
    resize: MemoryHeapResize | None = None,
    copyable: bool = True,
) -> str:
    lines: list[str] = []

    lines.append("h2. JVM Tuning Recommendation")
    if system_name:
        lines.append(f"*System:* {system_name}")
    if container.pod_name:
        lines.append(f"*Target pod:* {container.pod_name}")
    lines.append(f"*Target container:* {container.name}")
    if analysis.lifecycle_status and analysis.lifecycle_status != "tuning_attempted":
        lines.append(f"*Lifecycle status:* {analysis.lifecycle_status}")
    lines.append("")

    lines.append("h3. Ресурсы и JVM настройки контейнера (актуально для настройки)")
    rows = _resource_jvm_rows(
        container,
        analysis,
        resize=resize,
        copyable=copyable,
        runtime_metrics=runtime_metrics,
    )
    if rows:
        lines.append("|| Параметр || Было || Стало || Что делает ||")
        for name, was, to, purpose in rows:
            lines.append(f"| {name} | {was} | {to} | {purpose} |")
    else:
        lines.append("* Нет изменений ресурсов или JVM-флагов — смотрите рекомендации ниже.")
    lines.append("")

    lines.append("h3. Runtime Context")
    lines.append("|| Parameter || Value ||")
    lines.append(f"| JDK version | {_display(runtime_context.jdk_version)} |")
    lines.append(f"| Spring Boot version | {_display(runtime_context.spring_boot_version)} |")
    lines.append(f"| Heap used (MiB) | {_display(runtime_metrics.heap_used_mib)} |")
    lines.append(f"| OldGen used (MiB) | {_display(runtime_metrics.old_gen_used_mib)} |")
    lines.append(f"| OldGen capacity (MiB) | {_display(runtime_metrics.old_gen_capacity_mib)} |")
    lines.append(f"| GC pause p95 (ms) | {_display(runtime_metrics.gc_pause_p95_ms)} |")
    lines.append(f"| GC pause p99 (ms) | {_display(runtime_metrics.gc_pause_p99_ms)} |")
    lines.append(f"| GC time ratio (%) | {_display(runtime_metrics.gc_time_ratio_percent)} |")
    lines.append(f"| Container memory working set (MiB) | {_display(runtime_metrics.container_memory_working_set_mib)} |")
    lines.append(f"| Container memory limit (MiB) | {_display(container.limits.memory_mib)} |")
    lines.append("")

    lines.append("h3. Findings")
    if not analysis.findings:
        lines.append("* No critical findings detected.")
    else:
        for finding in analysis.findings:
            lines.append(f"* *[{finding.severity.upper()}]* {finding.code}: {finding.message}")
            for key, value in finding.details.items():
                lines.append(f"** {key}: {value}")
    lines.append("")

    lines.append("h3. Recommended Java Tool Options")
    if not analysis.recommendations:
        lines.append("* No recommendations.")
    else:
        for index, recommendation in enumerate(analysis.recommendations, start=1):
            lines.append(f"h4. Рекомендация {index}: {recommendation.title}")
            lines.append(f"*Rationale:* {recommendation.rationale}")
            lines.append(f"*Confidence:* {recommendation.confidence}")
            lines.append(f"*Evidence score:* {recommendation.evidence_score}/100")
            lines.append(f"*Risk score:* {recommendation.risk_score}/100")
            lines.append(f"*Expected gain:* {recommendation.expected_gain or 'N/A'}")
            lines.append(f"*Verification window:* {recommendation.verification_window}")
            lines.append(f"*Platform escalation required:* {'Yes' if recommendation.requires_platform_escalation else 'No'}")
            if recommendation.suggested_java_tool_options:
                flag_rows = _suggested_flag_rows(
                    recommendation.suggested_java_tool_options,
                    container.java_tool_options,
                    copyable=copyable,
                )
                if flag_rows:
                    lines.append("*Suggested options:*")
                    lines.append("|| Флаг || Было || Стало || Что делает ||")
                    for option, was, to, purpose in flag_rows:
                        lines.append(f"| {option} | {was} | {to} | {purpose} |")
            if recommendation.rollback_plan:
                lines.append("*Rollback plan:*")
                for step in recommendation.rollback_plan:
                    lines.append(f"** {step}")
            if recommendation.blocking_conditions:
                lines.append("*Blocking conditions:*")
                for blocker in recommendation.blocking_conditions:
                    lines.append(f"** {blocker}")
            if recommendation.notes:
                lines.append("*Notes:*")
                for note in recommendation.notes:
                    lines.append(f"** {note}")
            lines.append("")

    if analysis.memory_plan:
        lines.append("h3. Pod Memory Quota Plan")
        lines.append("|| Field || Value ||")
        lines.append(f"| Status | {analysis.memory_plan.status} |")
        lines.append(f"| Target container | {analysis.memory_plan.target_container} |")
        lines.append(f"| Requested delta (MiB) | {analysis.memory_plan.requested_delta_mib} |")
        if analysis.memory_plan.donor_suggestions:
            donors = ", ".join(f"{name}: {delta}MiB" for name, delta in analysis.memory_plan.donor_suggestions.items())
            lines.append(f"| Donor suggestions | {donors} |")
        else:
            lines.append("| Donor suggestions | - |")
        if analysis.memory_plan.notes:
            notes = " ".join(analysis.memory_plan.notes)
            lines.append(f"| Notes | {notes} |")

    if trend:
        lines.append("")
        lines.append("h3. Multi-run Stability")
        lines.append("|| Field || Value ||")
        lines.append(f"| Total runs | {trend.total_runs} |")
        lines.append(f"| Tuning effectiveness | {trend.tuning_effectiveness} |")
        lines.append(f"| Stable findings | {len(trend.stable_findings)} |")
        lines.append(f"| Regression findings | {len(trend.regression_findings)} |")
        lines.append("")
        if trend.stable_findings:
            lines.append("h4. Stable findings")
            lines.append("|| Severity || Code || Stability || Occurrences ||")
            for item in trend.stable_findings[:20]:
                lines.append(
                    f"| {item.severity} | {item.code} | {item.stability_ratio:.0%} | {item.occurrences}/{item.total_runs} |"
                )
            lines.append("")
        if trend.regression_findings:
            lines.append("h4. Regressions vs baseline")
            lines.append("|| Severity || Code || Stability ||")
            for item in trend.regression_findings[:20]:
                lines.append(f"| {item.severity} | {item.code} | {item.stability_ratio:.0%} |")
            lines.append("")

    lines.append("")
    lines.append("h3. Engineer Validation Runbook")
    for step in _build_validation_steps(analysis, runtime_metrics):
        lines.append(f"# {step}")

    lines.append("")
    lines.append("h3. Change Risks and Side Effects")
    for risk in _build_risk_notes(analysis):
        lines.append(f"* {risk}")

    lines.append("")
    lines.append("h3. Escalation Rule")
    lines.append("* If tuning recommendations do not improve GC/heap/memory-pressure metrics in the defined observation window, escalate to development team for heap dump and memory analysis.")

    return "\n".join(lines).strip() + "\n"


def _display(value: object) -> str:
    if value is None:
        return "N/A"
    return str(value)


def _cell(value: object) -> str:
    if value is None:
        return "не задан"
    return str(value)


def _resource_jvm_rows(
    container: ContainerResources,
    analysis: AnalysisResult,
    resize: MemoryHeapResize | None,
    copyable: bool,
    runtime_metrics: RuntimeMetrics | None = None,
) -> list[tuple[str, str, str, str]]:
    req_was, req_to, lim_was, lim_to = _recommended_memory_targets(
        container, analysis, resize, runtime_metrics
    )
    rows: list[tuple[str, str, str, str]] = []
    if _changed(req_was, req_to):
        rows.append(
            (
                "Memory request (MiB)",
                _cell(req_was),
                _cell(req_to),
                "Гарантированная память контейнера. Поднимите к фактическому working set, ниже limit.",
            )
        )
    if _changed(lim_was, lim_to):
        rows.append(
            (
                "Memory limit (MiB)",
                _cell(lim_was),
                _cell(lim_to),
                "Потолок RSS. Выше — OOMKilled.",
            )
        )
    current = _flag_value_map(container.java_tool_options)
    proposed: dict[str, str] = {}
    if copyable:
        for rec in analysis.recommendations:
            for flag in rec.suggested_java_tool_options or []:
                key, value = _split_flag_pair(flag)
                if not key:
                    continue
                proposed[key] = _display_flag_value(value) if value is not None else "включить"
    if resize and resize.resized:
        if (
            resize.max_ram_pct_to is not None
            and (
                resize.max_ram_pct_was is None
                or resize.max_ram_pct_to != resize.max_ram_pct_was
            )
        ):
            proposed["-XX:MaxRAMPercentage"] = f"{resize.max_ram_pct_to:g}"
        if resize.heap_mode == "xmx" and resize.xmx_to_mib is not None:
            proposed["-Xmx"] = f"{resize.xmx_to_mib}m"
    purposes = load_flag_purposes()
    seen: list[str] = []
    for key in list(current.keys()) + [key for key in proposed if key not in current]:
        if key in seen:
            continue
        seen.append(key)
        was = _display_flag_value(current[key]) if key in current else "не задан"
        to = proposed.get(key, was)
        if not _changed(was, to):
            continue
        rows.append((key, was, to, flag_purpose(key, purposes)))
    if (
        resize
        and resize.heap_mode in {"percent", "both"}
        and resize.xmx_was_mib is not None
        and _changed(resize.xmx_was_mib, resize.xmx_to_mib)
    ):
        rows.append(
            (
                "-Xmx (расчётный)",
                _fmt_mib(resize.xmx_was_mib),
                _fmt_mib(resize.xmx_to_mib),
                flag_purpose("-Xmx", purposes),
            )
        )
    return rows


def _recommended_memory_targets(
    container: ContainerResources,
    analysis: AnalysisResult,
    resize: MemoryHeapResize | None,
    runtime_metrics: RuntimeMetrics | None,
) -> tuple[int | None, int | None, int | None, int | None]:
    req_was = container.requests.memory_mib
    lim_was = container.limits.memory_mib
    req_to = req_was
    lim_to = lim_was
    if resize and resize.resized:
        req_to = resize.request_to
        lim_to = resize.limit_to
    if analysis.memory_plan and analysis.memory_plan.requested_delta_mib and lim_was:
        planned = lim_was + int(analysis.memory_plan.requested_delta_mib)
        if lim_to is None or planned > lim_to:
            lim_to = planned
    working = _working_set_mib(analysis, runtime_metrics)
    cap = lim_to if lim_to is not None else lim_was
    if _has_rule(analysis, "memory.request_pressure") and working is not None and req_was is not None:
        if working > req_was and (cap is None or working < cap):
            target = working if cap is None else min(working, cap)
            if req_to is None or target > req_to:
                req_to = target
    if _has_rule(analysis, "container.missing_memory_limit") and not lim_was:
        baseline = working if working is not None else req_to or req_was
        if baseline:
            lim_to = max(int(baseline * 1.25), 256)
            if req_was is None and working is not None:
                req_to = working
    if _has_rule(analysis, "container.request_limit_skew"):
        req_now = req_to if req_to is not None else req_was
        lim_now = lim_to if lim_to is not None else lim_was
        if req_now and lim_now and req_now / lim_now >= 0.95:
            lim_to = max(lim_now, int(req_now / 0.85))
    return req_was, req_to, lim_was, lim_to


def _working_set_mib(
    analysis: AnalysisResult,
    runtime_metrics: RuntimeMetrics | None,
) -> int | None:
    if runtime_metrics and runtime_metrics.container_memory_working_set_mib is not None:
        return int(runtime_metrics.container_memory_working_set_mib)
    for finding in analysis.findings:
        if finding.code != "memory.request_pressure":
            continue
        raw = (finding.details or {}).get("container_memory_working_set_mib")
        if raw in (None, ""):
            continue
        try:
            return int(float(str(raw)))
        except (TypeError, ValueError):
            return None
    return None


def _has_rule(analysis: AnalysisResult, code: str) -> bool:
    if any(finding.code == code for finding in analysis.findings):
        return True
    return any(code in (rec.rule_ids or []) for rec in analysis.recommendations)


def _changed(was: object, to: object) -> bool:
    return _cell(was) != _cell(to)


def _suggested_flag_rows(
    options: list[str],
    current_options: list[str] | None,
    *,
    copyable: bool,
) -> list[tuple[str, str, str, str]]:
    current_flags = _flag_value_map(current_options)
    purposes = load_flag_purposes()
    rows: list[tuple[str, str, str, str]] = []
    for option in options:
        key, new_val = _split_flag_pair(option)
        was = (
            _display_flag_value(current_flags[key])
            if key in current_flags
            else "не задан"
        )
        to = _display_flag_value(new_val) if new_val is not None else "включить"
        if not copyable and is_g1_flag_key(key):
            to = was
        if not _changed(was, to):
            continue
        rows.append((option, was, to, flag_purpose(key, purposes)))
    return rows


def _build_validation_steps(analysis: AnalysisResult, runtime_metrics: RuntimeMetrics) -> list[str]:
    finding_codes = {finding.code for finding in analysis.findings}
    steps: list[str] = [
        "Apply recommended JVM options in test/stage environment first. Keep previous values for rollback.",
        "Run at least one representative load profile and compare with baseline metrics collected before the change.",
    ]

    if "long_gc_pause" in finding_codes:
        steps.extend(
            [
                "Capture GC logs for 30-60 minutes after deployment and verify P95/P99 pause trend is below target.",
                "Check GC time ratio and confirm it decreases versus baseline (target: under 10-15%).",
            ]
        )

    if "old_gen_growth" in finding_codes:
        steps.extend(
            [
                "Track OldGen occupancy over time windows (15m/1h) and confirm old generation stabilizes after full/concurrent cycles.",
                "Validate that post-GC retained heap is not continuously increasing under similar traffic.",
            ]
        )

    if "memory_limit_pressure" in finding_codes:
        steps.extend(
            [
                "Validate container working set remains below 85-90% of memory limit in peak windows.",
                "Watch OOMKilled/restart counters and ensure there are no new memory-related restarts.",
            ]
        )
        if analysis.memory_plan and analysis.memory_plan.status == "needs_rebalance":
            steps.append(
                "If memory rebalance is used, monitor donor containers to ensure their P95 memory stays below 80-85% of new limits."
            )

    if runtime_metrics.heap_used_mib is not None:
        steps.append(
            f"Current heap spike observed: {runtime_metrics.heap_used_mib} MiB. Re-check that spikes fit with new headroom and do not trigger long GC pauses."
        )

    steps.append(
        "If no measurable improvement after the observation window, set status to tuning_not_effective and prepare escalation to dump analysis."
    )
    return steps


def _build_risk_notes(analysis: AnalysisResult) -> list[str]:
    finding_codes = {finding.code for finding in analysis.findings}
    risks: list[str] = [
        "Any JVM option change can shift latency and throughput profile; always compare SLA/SLO against baseline.",
    ]

    if "long_gc_pause" in finding_codes:
        risks.extend(
            [
                "Lowering GC pause targets may increase CPU usage due to more aggressive concurrent work.",
                "Changing G1 pacing parameters can reduce pause time but may increase allocation pressure in edge workloads.",
            ]
        )

    if "old_gen_growth" in finding_codes:
        risks.extend(
            [
                "Increasing reserve/free-ratio settings may reduce effective heap available to business objects.",
                "OldGen stabilization via tuning can mask true memory leaks; leak suspicion still requires dump analysis.",
            ]
        )

    if "memory_limit_pressure" in finding_codes:
        risks.extend(
            [
                "Raising container memory limit without pod budget can starve neighboring containers and cause cross-container instability.",
                "Reducing neighbor limits (rebalance) may introduce OOM risk for sidecars and service mesh proxies during bursts.",
            ]
        )
    if analysis.memory_plan and analysis.memory_plan.status == "needs_platform_escalation":
        risks.append(
            "If pod/namespace quota cannot be increased, tuning-only approach may hit a hard capacity ceiling."
        )

    risks.append(
        "Rollback plan is mandatory: keep previous JVM flags and resource limits so the team can revert quickly."
    )
    return risks

