#!/usr/bin/env python3
"""JVM diagnostic tree: copyable gate, HA 80%, no checkbox seeding.

Seam: evaluate_jvm_diagnostic_tree / apply_tree_gates / format_tree_wiki.
Adapter: run_jvm_analysis on DEMO_CounterAgent.
UI contract: tree questions present; presets and always-required metric copy gone.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JVM_SRC = ROOT / "jvmcheck_runtime" / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(JVM_SRC) not in sys.path:
    sys.path.insert(0, str(JVM_SRC))

from jvmcheck.diagnostic_tree import (  # noqa: E402
    TreeAnswers,
    apply_tree_gates,
    evaluate_jvm_diagnostic_tree,
    format_tree_wiki,
    load_playbook,
    propose_memory_heap_resize,
)
from jvmcheck.formatters.confluence_formatter import format_analysis_for_confluence  # noqa: E402
from jvmcheck.models import (  # noqa: E402
    AnalysisResult,
    ContainerResources,
    Finding,
    Recommendation,
    ResourceSpec,
    RuntimeContext,
    RuntimeMetrics,
)
from ui.jvm_runner import (  # noqa: E402
    create_jvm_system,
    list_jvm_systems,
    normalize_jvm_system_name,
    run_jvm_analysis,
)
from ui.models import JvmAnalyzeRequest, JvmTreeAnswers  # noqa: E402


def check(cond: bool, label: str, results: list[tuple[bool, str]]) -> None:
    results.append((bool(cond), label))


def _closed_tree(**overrides: object) -> TreeAnswers:
    data: dict[str, object] = {
        "pods_per_shoulder": 2,
        "restart_kind": "none",
        "cpu_throttled": "no",
        "gc_pause_p95_ms": 320.0,
        "user_latency_grew": "yes",
    }
    data.update(overrides)
    return TreeAnswers(**data)  # type: ignore[arg-type]


def test_playbook(results: list[tuple[bool, str]]) -> None:
    playbook = load_playbook()
    check(playbook["shoulders"] == 2, "playbook assumes two shoulders", results)
    check(playbook["ha_cpu_sum_pct_limit"] == 80, "playbook HA limit is 80", results)
    check(
        float(playbook["gc_pause_p95_ms"]) == 250.0,
        "playbook GC p95 threshold matches yaml default 250",
        results,
    )
    check(float(playbook["memory_sla_percent"]) == 80, "playbook memory SLA is 80%", results)
    check(
        float(playbook["memory_sla_not_critical_days"]) == 30,
        "playbook: time to 80% over 30 days is not critical",
        results,
    )
    check(float(playbook["memory_plateau_example_percent"]) == 75, "playbook plateau example is 75%", results)
    check(float(playbook["gc_pause_extreme_ms"]) == 16000, "playbook extreme GC pause is 16s", results)
    check(float(playbook["gc_pending_steep_percent"]) == 10, "playbook pending-GC steep growth is 10%", results)
    check(float(playbook["gc_pending_steep_hours"]) == 12, "playbook pending-GC watch window is 12h", results)
    check(float(playbook["gc_pending_low_usage_percent"]) == 40, "playbook pending-GC low usage is 40%", results)


def test_ha_headroom(results: list[tuple[bool, str]]) -> None:
    sticky_ok = evaluate_jvm_diagnostic_tree(
        _closed_tree(
            cpu_throttled="yes",
            cpu_pct_limits_shoulder_1=70.0,
            cpu_pct_limits_shoulder_2=10.0,
        )
    )
    check(sticky_ok.copyable_allowed, "70+10 sum 80 keeps copyable open", results)
    check(not sticky_ok.ha_exceeded, "70+10 is not HA breach", results)
    wiki_ok = format_tree_wiki(sticky_ok, _closed_tree(cpu_throttled="yes"))
    check(
        "баланс между кластерами" not in wiki_ok.lower(),
        "sticky 70/10 does not tell to fix cluster balancer",
        results,
    )

    breach = evaluate_jvm_diagnostic_tree(
        _closed_tree(
            cpu_throttled="yes",
            cpu_pct_limits_shoulder_1=50.0,
            cpu_pct_limits_shoulder_2=40.0,
        )
    )
    check(not breach.copyable_allowed, "50+40 sum 90 blocks copyable", results)
    check(breach.ha_exceeded, "50+40 is HA breach", results)
    check(
        any(f.code == "platform.ha_cpu_headroom" for f in breach.platform_findings),
        "HA breach emits platform.ha_cpu_headroom",
        results,
    )
    wiki_breach = format_tree_wiki(breach, _closed_tree(cpu_throttled="yes"))
    check("нет запаса на отказ плеча" in wiki_breach.lower(), "HA wiki names shoulder failover", results)
    check("g1" not in wiki_breach.lower() or "не g1" in wiki_breach.lower(), "HA wiki is not a G1 rec", results)

    missing = evaluate_jvm_diagnostic_tree(
        _closed_tree(cpu_throttled="yes", cpu_pct_limits_shoulder_1=40.0)
    )
    check(not missing.copyable_allowed, "missing second shoulder CPU does not downgrade throttle", results)
    check(not missing.ha_exceeded, "missing numbers are unknown, not a measured breach", results)


def test_unknown_and_oom_gates(results: list[tuple[bool, str]]) -> None:
    check(
        not evaluate_jvm_diagnostic_tree(_closed_tree(restart_kind="unknown")).copyable_allowed,
        "unknown restart blocks copyable",
        results,
    )
    check(
        not evaluate_jvm_diagnostic_tree(_closed_tree(cpu_throttled="unknown")).copyable_allowed,
        "unknown throttle blocks copyable",
        results,
    )
    check(
        not evaluate_jvm_diagnostic_tree(_closed_tree(gc_pause_p95_ms=None)).copyable_allowed,
        "missing GC p95 blocks copyable",
        results,
    )
    oom_open = evaluate_jvm_diagnostic_tree(
        _closed_tree(restart_kind="oomkilled", memory_cause_closed="no")
    )
    check(not oom_open.copyable_allowed, "open OOMKilled blocks copyable", results)
    check(oom_open.heap_branch_open, "OOMKilled opens heap branch", results)
    oom_closed = evaluate_jvm_diagnostic_tree(
        _closed_tree(restart_kind="oomkilled", memory_cause_closed="yes")
    )
    check(oom_closed.copyable_allowed, "closed OOM memory cause can copy", results)
    oome = evaluate_jvm_diagnostic_tree(
        _closed_tree(restart_kind="java_oome", memory_cause_closed="no")
    )
    check(not oome.copyable_allowed, "open Java OOME blocks copyable", results)
    check(oome.ask_memory_closed, "Java OOME asks whether memory cause is closed", results)
    check(oome.heap_branch_open, "Java OOME opens heap branch", results)
    unknown_cause = evaluate_jvm_diagnostic_tree(
        _closed_tree(restart_kind="oomkilled", memory_cause_closed="unknown")
    )
    check(not unknown_cause.copyable_allowed, "unknown memory-cause-closed blocks copyable", results)

    evicted = evaluate_jvm_diagnostic_tree(_closed_tree(restart_kind="evicted"))
    check(not evicted.copyable_allowed, "Evicted blocks copyable", results)
    check(
        any(f.code == "platform.evicted" for f in evicted.platform_findings),
        "Evicted emits platform finding",
        results,
    )


def test_gc_user_latency(results: list[tuple[bool, str]]) -> None:
    below = evaluate_jvm_diagnostic_tree(_closed_tree(gc_pause_p95_ms=200.0))
    check(not below.gc_analysis_required, "p95 200 is below 250: no GC analysis", results)
    check(not below.ask_user_latency, "below threshold does not ask user latency", results)
    check(below.copyable_allowed, "known-low p95 does not block master copyable", results)

    unknown_impact = evaluate_jvm_diagnostic_tree(
        _closed_tree(gc_pause_p95_ms=320.0, user_latency_grew="unknown")
    )
    check(unknown_impact.gc_analysis_required, "p95 320 requires GC analysis", results)
    check(unknown_impact.copyable_allowed, "known p95 keeps master copyable", results)
    check(not unknown_impact.pause_copyable_allowed, "unknown user impact blocks pause string", results)

    coincide = evaluate_jvm_diagnostic_tree(
        _closed_tree(
            cpu_throttled="yes",
            cpu_pct_limits_shoulder_1=30.0,
            cpu_pct_limits_shoulder_2=20.0,
            gc_pause_p95_ms=320.0,
            user_latency_grew="yes",
        )
    )
    check(coincide.ask_pauses_coincide_throttle, "throttle+GC analysis asks coincide", results)

    steal = evaluate_jvm_diagnostic_tree(
        _closed_tree(
            cpu_throttled="yes",
            cpu_pct_limits_shoulder_1=35.0,
            cpu_pct_limits_shoulder_2=30.0,
            gc_pause_p95_ms=400.0,
            user_latency_grew="yes",
            pauses_coincide_throttle="yes",
        )
    )
    check(steal.pauses_coincide_cpu, "pauses coinciding with throttle is CPU steal", results)
    check(steal.copyable_allowed, "HA 35+30 stays copyable for non-pause recs", results)
    check(not steal.pause_copyable_allowed, "coincide with throttle blocks pause JAVA_TOOL_OPTIONS", results)
    wiki_steal = format_tree_wiki(steal, _closed_tree(cpu_throttled="yes"))
    check("cpu limit" in wiki_steal.lower(), "coincide action raises CPU limit", results)
    check("maxgcpausemillis=200" not in wiki_steal.lower().split("{expand")[0], "coincide lead does not copy MaxGCPauseMillis=200", results)

    silent = evaluate_jvm_diagnostic_tree(
        _closed_tree(gc_pause_p95_ms=20000.0, user_latency_grew="no")
    )
    check(silent.long_gc_no_symptoms, "20s p95 without user latency is long GC without symptoms", results)
    check(silent.extreme_gc_pause, "20s p95 is extreme (>= 16s)", results)
    check(not silent.pause_copyable_allowed, "no user symptoms: do not copy pause flags", results)
    wiki_silent = format_tree_wiki(silent, _closed_tree(gc_pause_p95_ms=20000.0, user_latency_grew="no"))
    check("gc log" in wiki_silent.lower() and "safepoint" in wiki_silent.lower(), "silent long GC names extra analysis tools", results)
    check("liveness" in wiki_silent.lower() or "heartbeat" in wiki_silent.lower(), "silent long GC says when it becomes critical", results)
    check("javatooloptions: >" not in wiki_silent.lower().split("{expand")[0], "silent long GC lead has no copyable block", results)
    check("не копируйте" in wiki_silent.lower(), "silent long GC tells not to copy G1", results)

    at_threshold = evaluate_jvm_diagnostic_tree(_closed_tree(gc_pause_p95_ms=250.0))
    check(not at_threshold.gc_analysis_required, "p95 250 is not above threshold: no GC analysis", results)
    extreme_edge = evaluate_jvm_diagnostic_tree(
        _closed_tree(gc_pause_p95_ms=16000.0, user_latency_grew="no")
    )
    check(extreme_edge.extreme_gc_pause, "p95 16000 ms is the extreme boundary", results)
    check(extreme_edge.long_gc_no_symptoms, "16s without user latency is long GC without symptoms", results)

    coincide_no = evaluate_jvm_diagnostic_tree(
        _closed_tree(
            cpu_throttled="yes",
            cpu_pct_limits_shoulder_1=35.0,
            cpu_pct_limits_shoulder_2=30.0,
            gc_pause_p95_ms=320.0,
            user_latency_grew="yes",
            pauses_coincide_throttle="no",
        )
    )
    check(not coincide_no.pauses_coincide_cpu, "coincide=no is not CPU steal", results)
    check(coincide_no.pause_copyable_allowed, "throttle + coincide=no still allows pause copy", results)


def test_playbook_uncovered_branches(results: list[tuple[bool, str]]) -> None:
    playbook = load_playbook()
    fields = set(TreeAnswers.__dataclass_fields__)
    for item in playbook["questions"]:
        qid = str(item.get("id") or "")
        check(qid in fields, f"playbook question {qid} has a TreeAnswers field", results)

    growing_unknown = evaluate_jvm_diagnostic_tree(
        _closed_tree(heap_growing="unknown", gc_pause_p95_ms=80.0)
    )
    check(not growing_unknown.heap_branch_open, "unknown heap growth does not open heap branch", results)
    check(not growing_unknown.growth_plan_needed, "unknown heap growth has no SLA/OOM/plateau plan", results)

    gc_not_ran = evaluate_jvm_diagnostic_tree(
        _closed_tree(
            heap_growing="yes",
            gc_ran_in_window="no",
            heap_growth_percent=10.0,
            heap_growth_hours=12.0,
            gc_pause_p95_ms=80.0,
        )
    )
    wiki_not_ran = format_tree_wiki(gc_not_ran, _closed_tree(heap_growing="yes", gc_ran_in_window="no"))
    check(
        "ещё не утечка" in wiki_not_ran.lower() or "еще не утечка" in wiki_not_ran.lower(),
        "GC not ran: growth before first GC is not a leak",
        results,
    )
    check(gc_not_ran.steep_growth_pending_gc, "GC not ran at 10%/12h is steep pending first GC", results)
    check("ещё через 12 ч" in wiki_not_ran.lower() or "еще через 12 ч" in wiki_not_ran.lower(), "GC not ran: asks for a second 12h window", results)
    check(not gc_not_ran.heap_churn_not_leak, "GC not ran is not classified as churn", results)

    rss_pending = evaluate_jvm_diagnostic_tree(
        _closed_tree(
            pods_per_shoulder=1,
            heap_growing="yes",
            growth_of="container_rss",
            heap_growth_percent=10.0,
            heap_growth_hours=12.0,
            gc_ran_in_window="no",
            current_usage_percent=15.0,
            gc_pause_p95_ms=80.0,
        )
    )
    wiki_rss_pending = format_tree_wiki(
        rss_pending,
        _closed_tree(
            pods_per_shoulder=1,
            heap_growing="yes",
            growth_of="container_rss",
            heap_growth_percent=10.0,
            heap_growth_hours=12.0,
            gc_ran_in_window="no",
            current_usage_percent=15.0,
            gc_pause_p95_ms=80.0,
        ),
    )
    check(rss_pending.steep_growth_pending_gc, "RSS 10%/12h before first GC is steep", results)
    check(rss_pending.low_start_usage, "RSS 15% usage is still a low start", results)
    check("не поднимайте" in wiki_rss_pending.lower(), "low RSS start: do not raise limit yet", results)
    check("вторая точка" in wiki_rss_pending.lower() or "ещё через 12 ч" in wiki_rss_pending.lower(), "low RSS start: second snapshot", results)
    check("maxgcpausemillis" not in wiki_rss_pending.lower() or "не копируйте" in wiki_rss_pending.lower(), "low RSS start: does not copy G1", results)

    slow_pending = evaluate_jvm_diagnostic_tree(
        _closed_tree(
            heap_growing="yes",
            growth_of="container_rss",
            heap_growth_percent=1.0,
            heap_growth_hours=12.0,
            gc_ran_in_window="no",
            current_usage_percent=15.0,
            gc_pause_p95_ms=80.0,
        )
    )
    wiki_slow_pending = format_tree_wiki(slow_pending, _closed_tree(heap_growing="yes", gc_ran_in_window="no"))
    check(not slow_pending.steep_growth_pending_gc, "1%/12h before first GC is not steep", results)
    check("крутой наклон" not in wiki_slow_pending.lower(), "slow pending GC does not ask extra 12h watch", results)

    missing_rate = evaluate_jvm_diagnostic_tree(
        _closed_tree(heap_growing="yes", growth_of="heap", gc_pause_p95_ms=80.0)
    )
    wiki_rate = format_tree_wiki(missing_rate, _closed_tree(heap_growing="yes"))
    check("на сколько процентов" in wiki_rate.lower(), "growing without %/hours asks for rate", results)

    missing_gc_mib = evaluate_jvm_diagnostic_tree(
        _closed_tree(
            heap_growing="yes",
            gc_ran_in_window="yes",
            gc_pause_p95_ms=80.0,
        )
    )
    wiki_mib = format_tree_wiki(missing_gc_mib, _closed_tree(heap_growing="yes", gc_ran_in_window="yes"))
    check("сразу до и сразу после gc" in wiki_mib.lower(), "GC ran without MiB asks for before/after", results)

    at_sla = evaluate_jvm_diagnostic_tree(
        _closed_tree(
            heap_growing="yes",
            heap_growth_percent=1.0,
            heap_growth_hours=24.0,
            current_usage_percent=82.0,
            growth_of="container_rss",
            gc_pause_p95_ms=80.0,
        )
    )
    check(at_sla.hours_to_sla == 0.0, "usage 82% is already at SLA 80%", results)
    check(not at_sla.sla_not_critical, "already at SLA is not the 30-day not-critical path", results)
    wiki_sla = format_tree_wiki(at_sla, _closed_tree(heap_growing="yes"))
    check("уже на уровне sla 80%" in wiki_sla.lower(), "wiki says memory usage is already at SLA 80%", results)

    floor_stable = evaluate_jvm_diagnostic_tree(
        _closed_tree(
            heap_growing="yes",
            gc_ran_in_window="yes",
            heap_used_before_gc_mib=4000,
            heap_used_after_gc_mib=1200,
            post_gc_floor_rising="no",
            gc_pause_p95_ms=80.0,
        )
    )
    check(floor_stable.heap_churn_not_leak, "stable post-GC floor + drop is churn, not leak", results)
    check(not floor_stable.post_gc_floor_rising, "post_gc_floor=no is not rising", results)

    spike_no = evaluate_jvm_diagnostic_tree(
        _closed_tree(
            gc_pause_p95_ms=320.0,
            user_latency_grew="yes",
            gc_cpu_spike_sla="no",
        )
    )
    check(not spike_no.gc_cpu_spike_sla, "gc_cpu_spike=no is not a CPU-SLA finding", results)
    check(spike_no.pause_copyable_allowed, "gc_cpu_spike=no keeps pause copy when user latency grew", results)

    unknown_metric = evaluate_jvm_diagnostic_tree(
        _closed_tree(
            heap_growing="yes",
            growth_of="unknown",
            heap_growth_percent=5.0,
            heap_growth_hours=10.0,
            gc_pause_p95_ms=80.0,
        )
    )
    check(unknown_metric.accumulation_kind == "unknown", "unknown growth_of stays unclassified", results)
    check(unknown_metric.copyable_allowed, "unknown growth_of with low p95 stays copyable", results)

    missing_cpu = evaluate_jvm_diagnostic_tree(
        _closed_tree(cpu_throttled="yes", gc_pause_p95_ms=80.0)
    )
    check(not missing_cpu.copyable_allowed, "throttle without both shoulder CPU % blocks copyable", results)
    wiki_cpu = format_tree_wiki(missing_cpu, _closed_tree(cpu_throttled="yes"))
    check("cpu % of limits" in wiki_cpu.lower(), "missing shoulder CPU wiki names the metric", results)


def test_apply_gates_and_wiki(results: list[tuple[bool, str]]) -> None:
    analysis = AnalysisResult(
        findings=[
            Finding(code="gc.long_pause_p95", severity="warning", message="GC p95 pause exceeds threshold.")
        ],
        recommendations=[
            Recommendation(
                title="Reduce GC p95 pause",
                rationale="p95 pause is above profile threshold.",
                suggested_java_tool_options=[
                    "-XX:MaxGCPauseMillis=200",
                    "-XX:InitiatingHeapOccupancyPercent=30",
                ],
                rule_ids=["gc.long_pause_p95"],
            )
        ],
    )
    evaluation = evaluate_jvm_diagnostic_tree(
        _closed_tree(
            cpu_throttled="yes",
            cpu_pct_limits_shoulder_1=50.0,
            cpu_pct_limits_shoulder_2=40.0,
        )
    )
    gated = apply_tree_gates(analysis, evaluation)
    check(
        not any(rec.suggested_java_tool_options for rec in gated.recommendations),
        "HA breach strips copyable suggested flags",
        results,
    )
    check(
        any(f.code == "platform.ha_cpu_headroom" for f in gated.findings),
        "HA finding is attached to analysis",
        results,
    )
    wiki = format_tree_wiki(evaluation, _closed_tree(cpu_throttled="yes"), analysis=gated)
    check("javaToolOptions" not in wiki.split("{expand")[0], "lead-in has no copyable javaToolOptions", results)
    check("кандидат" in wiki.lower(), "blocked wiki keeps candidate table", results)
    check("|| Флаг || Было || Стало || Что делает || Что снять ||" in wiki, "candidate table has purpose column", results)
    check(
        "С какой доли заполнения heap G1 начинает concurrent cycle" in wiki,
        "IHOP candidate explains what the flag does",
        results,
    )
    check("почему так" in wiki.lower(), "wiki has evidence heading", results)
    check("что перепроверить" in wiki.lower(), "wiki has recheck heading", results)
    check("примените этот флаг" not in wiki.lower(), "blocked wiki does not say apply this flag", results)


def test_no_seed_without_number(results: list[tuple[bool, str]]) -> None:
    req = JvmAnalyzeRequest(
        system_name="DEMO_CounterAgent",
        container_name="application",
        tree=JvmTreeAnswers(
            pods_per_shoulder=2,
            restart_kind="none",
            cpu_throttled="no",
        ),
    )
    out = ROOT / "analysis_out_test" / "jvm_tree_no_seed"
    result = run_jvm_analysis(req, [], out)
    check(result.exit_code == 0, f"demo analysis runs without required metrics ({result.error})", results)
    wiki = (result.wiki_path.read_text(encoding="utf-8") if result.wiki_path else "")
    check("gc.long_pause_p95" not in wiki, "no p95 number does not seed gc.long_pause_p95", results)
    check("javaToolOptions: >" not in wiki, "missing p95 yields no copyable jvm-config block", results)


def test_copyable_when_gates_closed(results: list[tuple[bool, str]]) -> None:
    req = JvmAnalyzeRequest(
        system_name="DEMO_CounterAgent",
        container_name="application",
        gc_pause_p95_ms=320.0,
        tree=JvmTreeAnswers(
            pods_per_shoulder=2,
            restart_kind="none",
            cpu_throttled="no",
            user_latency_grew="yes",
        ),
    )
    out = ROOT / "analysis_out_test" / "jvm_tree_copyable"
    result = run_jvm_analysis(req, [], out)
    check(result.exit_code == 0, f"gated-open analysis runs ({result.error})", results)
    wiki = result.wiki_path.read_text(encoding="utf-8") if result.wiki_path else ""
    check("gc.long_pause_p95" in wiki, "p95 above threshold produces pause finding", results)
    check("javaToolOptions: >" in wiki, "closed gates keep copyable jvm-config block", results)
    check("config diff" not in wiki.lower(), "wiki does not duplicate was/to as Config diff notes", results)
    resources_block = wiki.split("h3. Ресурсы и JVM настройки", 1)[-1].split("h3. ", 1)[0]
    check("| -XX:+UseG1GC |" not in resources_block, "unchanged UseG1GC is omitted from resources table", results)


def test_heap_growth_without_restart(results: list[tuple[bool, str]]) -> None:
    growing = evaluate_jvm_diagnostic_tree(
        _closed_tree(restart_kind="none", heap_growing="yes", gc_pause_p95_ms=80.0)
    )
    check(growing.heap_branch_open, "heap growth without restart opens heap branch", results)
    check(not growing.ask_memory_closed, "no OOM: do not ask memory-cause-closed", results)
    check(growing.copyable_allowed, "low p95 + no restart + heap growth still copyable", results)
    quiet = evaluate_jvm_diagnostic_tree(
        _closed_tree(restart_kind="none", heap_growing="no", gc_pause_p95_ms=80.0)
    )
    check(not quiet.heap_branch_open, "no heap growth keeps heap branch closed", results)


def test_growth_investigation(results: list[tuple[bool, str]]) -> None:
    ttf = evaluate_jvm_diagnostic_tree(
        _closed_tree(
            heap_growing="yes",
            heap_growth_percent=10.0,
            heap_growth_hours=2.0,
            current_usage_percent=60.0,
            growth_of="oldgen",
            gc_pause_p95_ms=80.0,
        )
    )
    check(ttf.growth_rate_pct_per_hour == 5.0, "10% over 2h is 5%/h", results)
    check(ttf.hours_to_oom == 8.0, "60% current at 5%/h → 8h to OOM", results)
    check(not ttf.copyable_allowed, "TTF ≤ 24h blocks copyable JAVA_TOOL_OPTIONS", results)
    wiki = format_tree_wiki(ttf, _closed_tree(heap_growing="yes"))
    check("8 ч" in wiki, "wiki states hours to OOM", results)
    check("не цель паузы gc" not in wiki.lower(), "hours-to-OOM action does not say «не цель паузы GC»", results)
    check("oomkilled" in wiki.lower(), "hours-to-OOM action names OOMKilled", results)
    check("memory limit" in wiki.lower(), "hours-to-OOM action says to raise memory limit", results)

    churn = evaluate_jvm_diagnostic_tree(
        _closed_tree(
            heap_growing="yes",
            gc_ran_in_window="yes",
            heap_used_before_gc_mib=4000,
            heap_used_after_gc_mib=1200,
            gc_pause_p95_ms=80.0,
        )
    )
    check(churn.heap_churn_not_leak, "4000→1200 after GC is churn, not leak", results)
    check(abs((churn.gc_retained_ratio or 0) - 0.3) < 1e-9, "retained ratio 1200/4000 = 0.3", results)
    wiki_churn = format_tree_wiki(churn, _closed_tree(heap_growing="yes"))
    check("вслепую" not in wiki_churn.lower(), "churn action does not say «вслепую»", results)
    check("-xmx" in wiki_churn.lower() and "не поднимайте" in wiki_churn.lower(), "churn action says not to raise -Xmx", results)
    check("не со старта пода" in wiki_churn.lower(), "growth wiki footnotes rate is not from pod start", results)
    check("плато" in wiki_churn.lower(), "growth wiki has SLA/OOM/plateau plan", results)
    check("перезапустит под" in wiki_churn.lower(), "growth wiki warns that settings change restarts the pod", results)

    ratchet = evaluate_jvm_diagnostic_tree(
        _closed_tree(
            heap_growing="yes",
            gc_ran_in_window="yes",
            heap_used_before_gc_mib=4000,
            heap_used_after_gc_mib=1200,
            post_gc_floor_rising="yes",
            gc_pause_p95_ms=80.0,
        )
    )
    check(not ratchet.heap_churn_not_leak, "rising post-GC floor is not harmless churn", results)
    check(ratchet.post_gc_floor_rising, "rising post-GC floor is flagged", results)
    wiki_ratchet = format_tree_wiki(ratchet, _closed_tree(heap_growing="yes", post_gc_floor_rising="yes"))
    check("проблема всё ещё есть" in wiki_ratchet.lower() or "проблема все еще есть" in wiki_ratchet.lower(), "rising floor says the problem remains", results)
    check("это не утечка" not in wiki_ratchet.lower(), "rising floor does not say «это не утечка»", results)
    check("histogram" in wiki_ratchet.lower(), "rising floor still asks for histogram", results)

    spike = evaluate_jvm_diagnostic_tree(
        _closed_tree(
            heap_growing="yes",
            gc_ran_in_window="yes",
            heap_used_before_gc_mib=4000,
            heap_used_after_gc_mib=1200,
            gc_cpu_spike_sla="yes",
            gc_pause_p95_ms=400.0,
            user_latency_grew="yes",
        )
    )
    check(spike.gc_cpu_spike_sla, "large cleanup CPU spike is flagged", results)
    check(not spike.pause_copyable_allowed, "CPU spike during GC blocks pause copy", results)
    wiki_spike = format_tree_wiki(spike, _closed_tree(gc_cpu_spike_sla="yes"))
    check("cpu" in wiki_spike.lower() and "sla" in wiki_spike.lower(), "CPU spike action names SLA", results)
    check("не поднимайте" in wiki_spike.lower() and "-xmx" in wiki_spike.lower(), "CPU spike says not to raise -Xmx", results)

    retained = evaluate_jvm_diagnostic_tree(
        _closed_tree(
            heap_growing="yes",
            gc_ran_in_window="yes",
            heap_used_before_gc_mib=4000,
            heap_used_after_gc_mib=3800,
            gc_pause_p95_ms=80.0,
        )
    )
    check(not retained.heap_churn_not_leak, "4000→3800 after GC is retained", results)
    check(abs((retained.gc_retained_ratio or 0) - 0.95) < 1e-9, "retained ratio 3800/4000 = 0.95", results)
    wiki_retained = format_tree_wiki(retained, _closed_tree(heap_growing="yes"))
    check("histogram" in wiki_retained.lower(), "retained heap tells to take histogram or dump", results)
    check("топ классов" in wiki_retained.lower(), "retained heap says what to look at in histogram", results)
    check("разработке" in wiki_retained.lower(), "retained heap says to hand the class list to developers", results)
    check("не цель паузы gc" not in wiki_retained.lower(), "retained heap does not say «не цель паузы GC»", results)
    check("не крутите g1" not in wiki_retained.lower(), "retained heap does not say «не крутите G1»", results)

    native = evaluate_jvm_diagnostic_tree(
        _closed_tree(
            heap_growing="yes",
            growth_of="container_rss",
            gc_ran_in_window="yes",
            heap_used_before_gc_mib=4000,
            heap_used_after_gc_mib=1200,
            gc_pause_p95_ms=80.0,
        )
    )
    check(not native.copyable_allowed, "RSS growth with reclaimed heap blocks copyable", results)
    check(native.non_heap_accumulation, "RSS growth + heap drop after GC is non-heap", results)
    gated_native = apply_tree_gates(AnalysisResult(), native)
    check(
        any(rec.title == "Увеличьте memory limit контейнера" for rec in gated_native.recommendations),
        "healthy GC + RSS growth emits raise-limit recommendation",
        results,
    )
    wiki_native = format_tree_wiki(native, _closed_tree(heap_growing="yes"))
    check("javatooloptions: >" not in wiki_native.lower().split("{expand")[0], "non-heap lead-in has no copyable block", results)
    check("увеличьте memory limit" in wiki_native.lower(), "healthy GC + RSS growth tells to raise memory limit", results)
    check("gc отработал нормально" in wiki_native.lower(), "wiki says GC worked, not 'оборот'", results)
    check("оборот" not in wiki_native.lower(), "wiki does not say оборот", results)
    check("-xmx" in wiki_native.lower() and "maxrampercentage" in wiki_native.lower(), "wiki asks Xmx vs MaxRAMPercentage", results)
    check("| да |" in wiki_native.lower() or "|да|" in wiki_native.lower().replace(" ", ""), "wiki prints да, not yes", results)
    check("directbytebuffer" not in wiki_native.lower(), "wiki does not mention DirectByteBuffer", results)
    check("metaspace" not in wiki_native.lower(), "wiki does not mention Metaspace", results)

    heap_from_rss = evaluate_jvm_diagnostic_tree(
        _closed_tree(
            heap_growing="yes",
            growth_of="container_rss",
            gc_ran_in_window="yes",
            heap_used_before_gc_mib=4000,
            heap_used_after_gc_mib=3800,
            gc_pause_p95_ms=80.0,
        )
    )
    check(not heap_from_rss.non_heap_accumulation, "RSS growth with retained heap is Java heap", results)

    far = evaluate_jvm_diagnostic_tree(
        _closed_tree(
            heap_growing="yes",
            heap_growth_percent=1.0,
            heap_growth_hours=24.0,
            current_usage_percent=50.0,
            growth_of="container_rss",
            gc_pause_p95_ms=80.0,
        )
    )
    check(far.copyable_allowed, "slow growth far from OOM stays copyable", results)
    check(abs((far.hours_to_oom or 0) - 1200.0) < 1e-6, "50% remaining at 1%/24h → 1200h", results)
    check(abs((far.hours_to_sla or 0) - 720.0) < 1e-6, "50%→80% at 1%/24h is exactly 30 days", results)
    check(not far.sla_not_critical, "exactly 30 days to SLA 80% is still inside the window", results)

    slow = evaluate_jvm_diagnostic_tree(
        _closed_tree(
            heap_growing="yes",
            heap_growth_percent=1.0,
            heap_growth_hours=12.0,
            current_usage_percent=15.0,
            growth_of="container_rss",
            gc_ran_in_window="yes",
            heap_used_before_gc_mib=600,
            heap_used_after_gc_mib=400,
            oldgen_returned_after_gc="yes",
            gc_pause_p95_ms=80.0,
        )
    )
    check(slow.non_heap_accumulation, "1%/12h RSS + healthy GC is still non-heap", results)
    check(abs((slow.hours_to_sla or 0) - 780.0) < 1e-6, "15%→80% at 1%/12h → 780h (~32.5 days)", results)
    check(slow.sla_not_critical, "time to SLA 80% over 30 days is not critical", results)
    check(slow.copyable_allowed, "sub-SLA slow RSS does not block JAVA_TOOL_OPTIONS", results)
    gated_slow = apply_tree_gates(AnalysisResult(), slow)
    check(
        not any(rec.title == "Увеличьте memory limit контейнера" for rec in gated_slow.recommendations),
        "sub-SLA slow RSS does not emit raise-limit recommendation",
        results,
    )
    wiki_slow = format_tree_wiki(slow, _closed_tree(heap_growing="yes"))
    check("не критично" in wiki_slow.lower(), "sub-SLA wiki says not critical", results)
    check("30 дн" in wiki_slow.lower() or "30 дней" in wiki_slow.lower(), "sub-SLA wiki names 30-day horizon", results)
    check("увеличьте memory limit" not in wiki_slow.lower(), "sub-SLA wiki does not raise memory limit", results)

    edge = evaluate_jvm_diagnostic_tree(
        _closed_tree(
            heap_growing="yes",
            heap_growth_percent=1.0,
            heap_growth_hours=12.0,
            current_usage_percent=20.0,
            growth_of="container_rss",
            gc_ran_in_window="yes",
            heap_used_before_gc_mib=600,
            heap_used_after_gc_mib=400,
            oldgen_returned_after_gc="yes",
            gc_pause_p95_ms=80.0,
        )
    )
    check(abs((edge.hours_to_sla or 0) - 720.0) < 1e-6, "20%→80% at 1%/12h is exactly 30 days", results)
    check(not edge.sla_not_critical, "exactly 30 days to 80% stays critical for non-heap", results)
    check(not edge.copyable_allowed, "exactly 30 days to SLA still blocks copyable for non-heap", results)


def test_resize_and_was_to_tables(results: list[tuple[bool, str]]) -> None:
    resize = propose_memory_heap_resize(
        limit_mib=8192,
        request_mib=4096,
        java_tool_options=["-XX:MaxRAMPercentage=70"],
        need_resize=True,
    )
    check(resize.resized, "percent mode emits a resize", results)
    check(resize.limit_was == 8192 and resize.limit_to == 9420, "limit 8192 → 9420", results)
    check(resize.request_was == 4096 and resize.request_to == 5324, "request 4096 → 5324", results)
    check(resize.xmx_was_mib == 5734 and resize.xmx_to_mib == 6594, "calculated Xmx 5734 → 6594", results)

    xmx_resize = propose_memory_heap_resize(
        limit_mib=8192,
        request_mib=4096,
        java_tool_options=["-Xmx2048m"],
        need_resize=True,
    )
    check(xmx_resize.xmx_to_mib == 2355, "integer -Xmx scales with limit 2048→2355", results)

    native = evaluate_jvm_diagnostic_tree(
        _closed_tree(
            heap_growing="yes",
            growth_of="container_rss",
            gc_ran_in_window="yes",
            heap_used_before_gc_mib=600,
            heap_used_after_gc_mib=400,
            oldgen_returned_after_gc="yes",
            gc_pause_p95_ms=80.0,
        )
    )
    wiki = format_tree_wiki(
        native,
        _closed_tree(heap_growing="yes"),
        java_tool_options=["-XX:MaxRAMPercentage=70"],
        resize=resize,
    )
    check("5324" in wiki and "9420" in wiki, "MaxRAMPercentage sentence includes request/limit targets", results)
    check("5734" in wiki and "6594" in wiki, "MaxRAMPercentage sentence includes calculated -Xmx was/to", results)
    check("выставить: memory request 5324 mib" in wiki.lower(), "action names request to set", results)

    container = ContainerResources(
        name="application",
        requests=ResourceSpec(cpu_millicores=2000, memory_mib=4096),
        limits=ResourceSpec(cpu_millicores=4000, memory_mib=8192),
        java_tool_options=[
            "-XX:MaxRAMPercentage=70.0",
            "-XX:InitiatingHeapOccupancyPercent=45",
        ],
    )
    confluence = format_analysis_for_confluence(
        analysis=AnalysisResult(
            recommendations=[
                Recommendation(
                    title="Reduce GC p95 pause",
                    rationale="p95 pause is above profile threshold.",
                    suggested_java_tool_options=["-XX:InitiatingHeapOccupancyPercent=30"],
                )
            ]
        ),
        container=container,
        runtime_metrics=RuntimeMetrics(),
        runtime_context=RuntimeContext(),
        resize=resize,
        copyable=True,
    )
    check(
        "|| Параметр || Было || Стало || Что делает ||" in confluence,
        "resources table has было/стало/что делает",
        results,
    )
    check("| Memory request (MiB) | 4096 | 5324 |" in confluence, "resources table shows request was/to", results)
    check("| Memory limit (MiB) | 8192 | 9420 |" in confluence, "resources table shows limit was/to", results)
    resources_block = confluence.split("h3. Ресурсы и JVM настройки", 1)[1].split("h3. ", 1)[0]
    check("| -XX:MaxRAMPercentage |" not in resources_block, "unchanged MaxRAMPercentage is omitted from resources table", results)
    check("CPU request" not in resources_block, "unchanged CPU request is omitted from resources table", results)
    check("| 45 | 30 |" in confluence, "IHOP recommendation shows was 45 and to 30", results)
    check(
        "С какой доли заполнения heap G1 начинает concurrent cycle" in confluence,
        "IHOP row explains what the parameter does",
        results,
    )
    check("тюнинг уже пробовали" not in confluence, "default lifecycle is not shown as «тюнинг уже пробовали»", results)
    check("*Lifecycle status:*" not in confluence, "default tuning_attempted is omitted from wiki", results)
    check("*Статус жизненного цикла:*" not in confluence, "lifecycle jargon is omitted from wiki", results)

    request_pressure = format_analysis_for_confluence(
        analysis=AnalysisResult(
            findings=[
                Finding(
                    code="memory.request_pressure",
                    severity="warning",
                    message="Working set significantly exceeds memory request.",
                    details={
                        "container_memory_working_set_mib": "5200",
                        "container_memory_request_mib": "4096",
                    },
                )
            ],
            recommendations=[
                Recommendation(
                    title="Raise container memory request to match actual usage",
                    rationale="Working set is above memory request.",
                    rule_ids=["memory.request_pressure"],
                )
            ],
        ),
        container=container,
        runtime_metrics=RuntimeMetrics(container_memory_working_set_mib=5200),
        runtime_context=RuntimeContext(),
        copyable=True,
    )
    check(
        "| Memory request (MiB) | 4096 | 5200 |" in request_pressure,
        "request_pressure rec appears in resources table as 4096→5200",
        results,
    )
    pressure_block = request_pressure.split("h3. Ресурсы и JVM настройки", 1)[1].split("h3. ", 1)[0]
    check("| Memory limit (MiB) |" not in pressure_block, "request_pressure does not invent a limit change", results)

    skew = format_analysis_for_confluence(
        analysis=AnalysisResult(
            findings=[
                Finding(
                    code="container.request_limit_skew",
                    severity="warning",
                    message="Container memory request is too close to memory limit.",
                )
            ],
            recommendations=[
                Recommendation(
                    title="Request-to-limit ratio is too tight",
                    rationale="Memory request is almost equal to limit.",
                    rule_ids=["container.request_limit_skew"],
                )
            ],
        ),
        container=ContainerResources(
            name="application",
            requests=ResourceSpec(memory_mib=7800),
            limits=ResourceSpec(memory_mib=8192),
        ),
        runtime_metrics=RuntimeMetrics(),
        runtime_context=RuntimeContext(),
        copyable=True,
    )
    check("| Memory limit (MiB) | 8192 | 9176 |" in skew, "request_limit_skew raises limit for headroom", results)

    req = JvmAnalyzeRequest(
        system_name="DEMO_CounterAgent",
        container_name="application",
        gc_pause_p95_ms=80.0,
        tree=JvmTreeAnswers(
            pods_per_shoulder=2,
            restart_kind="none",
            cpu_throttled="no",
            heap_growing="yes",
            growth_of="container_rss",
            heap_growth_percent=10.0,
            heap_growth_hours=12.0,
            gc_ran_in_window="yes",
            heap_used_before_gc_mib=600,
            heap_used_after_gc_mib=400,
            oldgen_returned_after_gc="yes",
        ),
    )
    out = ROOT / "analysis_out_test" / "jvm_tree_resize_demo"
    result = run_jvm_analysis(req, [], out)
    check(result.exit_code == 0, f"DEMO resize analysis runs ({result.error})", results)
    demo_wiki = result.wiki_path.read_text(encoding="utf-8") if result.wiki_path else ""
    check("5324" in demo_wiki and "9420" in demo_wiki, "DEMO wiki has request/limit targets", results)
    check(
        "|| Параметр || Было || Стало || Что делает ||" in demo_wiki,
        "DEMO wiki resources table has было/стало",
        results,
    )
    check("| Memory request (MiB) | 4096 | 5324 |" in demo_wiki, "DEMO wiki request was 4096 became 5324", results)


def test_ui_contract(results: list[tuple[bool, str]]) -> None:
    html = (ROOT / "ui" / "web" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "ui" / "web" / "js" / "app.js").read_text(encoding="utf-8")
    check("jvm-preset-btn" not in html, "HTML has no JVM preset buttons", results)
    check('id="jvm-problem-list"' not in html, "HTML has no problem checkbox list", results)
    check("Обязательные поля: `GC p95`" not in html, "HTML dropped always-required metric copy", results)
    check('id="jvm-system-name"' in html, "HTML has system selector", results)
    check('id="jvm-pods-per-shoulder"' in html, "HTML asks pods per shoulder", results)
    check('id="jvm-new-system-name"' in html, "HTML has new-system name field", results)
    check('id="jvm-create-system-btn"' in html, "HTML has load-new-system button", results)
    check("добавить новую систему" in js, "JS offers add-new-system option", results)
    check('"__new__"' in js or "'__new__'" in js, "JS uses __new__ sentinel for add-new-system", results)
    check("/api/jvm/systems" in js, "JS posts new system to /api/jvm/systems", results)
    check('id="jvm-restart-kind"' in html, "HTML asks restart kind", results)
    check('id="jvm-cpu-throttled"' in html, "HTML asks CPU throttle", results)
    check('id="jvm-cpu-pct-shoulder-1"' in html, "HTML asks shoulder 1 CPU % of limits", results)
    check('id="jvm-cpu-pct-shoulder-2"' in html, "HTML asks shoulder 2 CPU % of limits", results)
    check('id="jvm-user-latency-grew"' in html, "HTML asks user latency impact", results)
    check('id="jvm-heap-growing"' in html, "HTML asks about heap growth without restart", results)
    check('id="jvm-oldgen-mode"' in html, "HTML lets choose OldGen as % or MiB", results)
    check('id="jvm-oldgen-used-mib"' in html, "HTML has integer OldGen used MiB", results)
    check('id="jvm-heap-growth-hours"' in html, "HTML asks growth window in hours", results)
    check('id="jvm-heap-before-gc"' in html, "HTML asks heap used before GC", results)
    check('id="jvm-heap-after-gc"' in html, "HTML asks heap used after GC", results)
    check('id="jvm-accumulation-space"' not in html, "HTML does not ask heap vs non-heap", results)
    check('id="jvm-direct-buffers"' not in html, "HTML does not ask DirectByteBuffer", results)
    check('id="jvm-gc-kind"' not in html, "HTML does not ask which GC kind", results)
    check("это отсечение, не тупик" not in html, "HTML dropped opaque p95 cutoff jargon", results)
    check("Если p95 не выше 250" in html, "HTML p95 help names the threshold", results)
    check("больше 30 дней" in html, "HTML SLA help: over 30 days is not critical", results)
    check("80%" in html, "HTML names memory SLA 80%", results)
    check("не со старта пода" in html or "не от старта пода" in html, "HTML footnotes growth rate is not from pod start", results)
    check("знаменатель" not in html, "HTML does not say «знаменатель»", results)
    check("warmup" not in html.lower(), "HTML does not say warmup", results)
    check("прогрев и заполнение кэша" in html.lower(), "HTML says warmup/cache are outside the rate window", results)
    check('id="jvm-post-gc-floor"' in html, "HTML asks whether post-GC floor is rising", results)
    check('id="jvm-gc-cpu-spike"' in html, "HTML asks whether GC cleanup CPU spike hit SLA", results)
    check("post_gc_floor_rising" in js, "JS sends rising post-GC floor", results)
    check("gc_cpu_spike_sla" in js, "JS sends GC CPU spike vs SLA", results)
    check("оборот молодого поколения" not in html, "HTML does not say оборот", results)
    check("heap_growth_hours" in js, "JS sends growth hours", results)
    check("old_gen_used_mib" in js, "JS can send OldGen as integer MiB", results)
    check('id="jvm-review-confirm"' not in html, "HTML does not require confirm checkbox to run", results)
    check("jvm-fill-last-values-btn" in html, "fill-last still exists", results)
    check(
        html.find("jvm-advanced-settings") < html.find("jvm-fill-last-values-btn"),
        "fill-last lives in advanced, not in the tree",
        results,
    )
    check("подтвердите запуск" not in js, "JS does not block Analyze on confirm checkbox", results)
    check("JVM_PRESET_DEFAULTS" not in js, "JS dropped preset defaults", results)
    check("заполните обязательные поля: GC p95" not in js, "JS dropped always-required metric error", results)
    check("tree:" in js or "pods_per_shoulder" in js, "JS sends tree answers", results)


def test_create_jvm_system(results: list[tuple[bool, str]]) -> None:
    try:
        normalize_jvm_system_name("")
        check(False, "empty system name is rejected", results)
    except ValueError:
        check(True, "empty system name is rejected", results)
    try:
        normalize_jvm_system_name("__new__")
        check(False, "__new__ system name is reserved", results)
    except ValueError:
        check(True, "__new__ system name is reserved", results)
    try:
        normalize_jvm_system_name("DEMO_CounterAgent")
        check(False, "demo system name is reserved", results)
    except ValueError:
        check(True, "demo system name is reserved", results)
    try:
        normalize_jvm_system_name("../escape")
        check(False, "path in system name is rejected", results)
    except ValueError:
        check(True, "path in system name is rejected", results)

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        (root / "resources").mkdir()
        upload = root / "resources.yaml"
        upload.write_text(
            "app:\n"
            "  application:\n"
            "    resources:\n"
            "      requests:\n"
            "        memory: 1Gi\n"
            "      limits:\n"
            "        memory: 2Gi\n",
            encoding="utf-8",
        )
        created = create_jvm_system("NewTestAS", [upload], root=root)
        check(created["system"] == "NewTestAS", "create_jvm_system returns the new name", results)
        check(created["created"] is True, "create_jvm_system reports created=True", results)
        saved = root / "resources" / "NewTestAS" / "resources.yaml"
        check(saved.is_file(), "create_jvm_system writes resources.yaml under the AS folder", results)
        check("NewTestAS" in list_jvm_systems(root=root), "new AS appears in the system list", results)
        try:
            create_jvm_system("NewTestAS", [root / "notes.txt"], root=root)
            check(False, "create without resources.yaml is rejected", results)
        except ValueError:
            check(True, "create without resources.yaml is rejected", results)


def main() -> int:
    results: list[tuple[bool, str]] = []
    test_playbook(results)
    test_ha_headroom(results)
    test_unknown_and_oom_gates(results)
    test_gc_user_latency(results)
    test_playbook_uncovered_branches(results)
    test_heap_growth_without_restart(results)
    test_growth_investigation(results)
    test_apply_gates_and_wiki(results)
    test_resize_and_was_to_tables(results)
    test_ui_contract(results)
    test_create_jvm_system(results)
    test_no_seed_without_number(results)
    test_copyable_when_gates_closed(results)
    failed = [(ok, label) for ok, label in results if not ok]
    for ok, label in results:
        print(("OK  " if ok else "FAIL") + " " + label)
    if failed:
        print(f"FAIL: {len(failed)}/{len(results)}", file=sys.stderr)
        return 1
    print(f"OK: {len(results)} checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
