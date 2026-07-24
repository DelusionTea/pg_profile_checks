from __future__ import annotations

from jvmcheck.models import AnalysisResult, ContainerResources, MultiRunAnalysis, RuntimeContext, RuntimeMetrics


def format_analysis_for_confluence(
    analysis: AnalysisResult,
    container: ContainerResources,
    runtime_metrics: RuntimeMetrics,
    runtime_context: RuntimeContext,
    system_name: str | None = None,
    trend: MultiRunAnalysis | None = None,
) -> str:
    lines: list[str] = []

    lines.append("h2. JVM Tuning Recommendation")
    if system_name:
        lines.append(f"*System:* {system_name}")
    if container.pod_name:
        lines.append(f"*Target pod:* {container.pod_name}")
    lines.append(f"*Target container:* {container.name}")
    lines.append(f"*Lifecycle status:* {analysis.lifecycle_status}")
    lines.append("")

    lines.append("h3. Ресурсы и JVM настройки контейнера (актуально для настройки)")
    lines.append("|| Parameter || Value ||")
    lines.append(f"| CPU request (m) | {_display(container.requests.cpu_millicores)} |")
    lines.append(f"| CPU limit (m) | {_display(container.limits.cpu_millicores)} |")
    lines.append(f"| Memory request (MiB) | {_display(container.requests.memory_mib)} |")
    lines.append(f"| Memory limit (MiB) | {_display(container.limits.memory_mib)} |")
    lines.append(
        f"| Ephemeral storage request (MiB) | {_display(container.requests.ephemeral_storage_mib)} |"
    )
    lines.append(
        f"| Ephemeral storage limit (MiB) | {_display(container.limits.ephemeral_storage_mib)} |"
    )
    lines.append(
        f"| Current JVM options | {_display_jvm_options(container.java_tool_options)} |"
    )
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
            lines.append(f"h4. Recommendation {index}: {recommendation.title}")
            lines.append(f"*Rationale:* {recommendation.rationale}")
            lines.append(f"*Confidence:* {recommendation.confidence}")
            lines.append(f"*Evidence score:* {recommendation.evidence_score}/100")
            lines.append(f"*Risk score:* {recommendation.risk_score}/100")
            lines.append(f"*Expected gain:* {recommendation.expected_gain or 'N/A'}")
            lines.append(f"*Verification window:* {recommendation.verification_window}")
            lines.append(f"*Platform escalation required:* {'Yes' if recommendation.requires_platform_escalation else 'No'}")
            if recommendation.suggested_java_tool_options:
                lines.append("*Suggested options:*")
                for option in recommendation.suggested_java_tool_options:
                    lines.append(f"** {{code}}{option}{{code}}")
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


def _display_jvm_options(options: list[str]) -> str:
    if not options:
        return "N/A"
    return "{{code}}" + " ".join(options) + "{{code}}"


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

