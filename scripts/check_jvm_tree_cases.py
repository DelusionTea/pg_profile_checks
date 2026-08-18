#!/usr/bin/env python3
"""Typical JVM-tree cases on DEMO_CounterAgent: gate, action, numbers.

DEMO application: request 4096 MiB, limit 8192 MiB, MaxRAMPercentage=70,
MaxGCPauseMillis=250. Resize step = max(15% of limit, 256) = 1228 MiB
→ request 5324, limit 9420, calculated -Xmx 5734 → 6594.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JVM_SRC = ROOT / "jvmcheck_runtime" / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(JVM_SRC) not in sys.path:
    sys.path.insert(0, str(JVM_SRC))

from jvmcheck.diagnostic_tree import propose_memory_heap_resize  # noqa: E402
from ui.jvm_runner import run_jvm_analysis  # noqa: E402
from ui.models import JvmAnalyzeRequest, JvmTreeAnswers  # noqa: E402

OUT = ROOT / "analysis_out_test" / "jvm_tree_cases"


def check(cond: bool, label: str, results: list[tuple[bool, str]]) -> None:
    results.append((bool(cond), label))


def _base_tree(**overrides: object) -> JvmTreeAnswers:
    data: dict[str, object] = {
        "pods_per_shoulder": 2,
        "restart_kind": "none",
        "cpu_throttled": "no",
        "heap_growing": "no",
    }
    data.update(overrides)
    return JvmTreeAnswers(**data)  # type: ignore[arg-type]


def _run(name: str, req: JvmAnalyzeRequest) -> tuple[int, str, str | None]:
    result = run_jvm_analysis(req, [], OUT / name)
    wiki = result.wiki_path.read_text(encoding="utf-8") if result.wiki_path else ""
    return result.exit_code, wiki, result.error


def _lead(wiki: str) -> str:
    return wiki.split("{expand", 1)[0].lower()


def _resources(wiki: str) -> str:
    if "h3. Ресурсы и JVM настройки" not in wiki:
        return ""
    return wiki.split("h3. Ресурсы и JVM настройки", 1)[1].split("h3. ", 1)[0]


def test_rss_gc_healthy(results: list[tuple[bool, str]]) -> None:
    """RSS растёт, GC отрабатывает: поднимать limit/request, не G1."""
    code, wiki, err = _run(
        "rss_gc_healthy",
        JvmAnalyzeRequest(
            system_name="DEMO_CounterAgent",
            container_name="application",
            gc_pause_p95_ms=80.0,
            container_memory_usage_percent=70.0,
            tree=_base_tree(
                heap_growing="yes",
                growth_of="container_rss",
                heap_growth_percent=10.0,
                heap_growth_hours=12.0,
                gc_ran_in_window="yes",
                heap_used_before_gc_mib=600,
                heap_used_after_gc_mib=400,
                oldgen_returned_after_gc="yes",
            ),
        ),
    )
    lead = _lead(wiki)
    res = _resources(wiki)
    check(code == 0, f"rss_gc_healthy runs ({err})", results)
    check("javatooloptions: >" not in lead, "rss: no copyable jvm-config", results)
    check("увеличьте memory request" in lead, "rss: action raises request", results)
    check("| Memory request (MiB) | 4096 | 5734 |" in res, "rss: request follows working set 5734 (above resize step 5324)", results)
    check("| Memory limit (MiB) | 8192 | 9420 |" in res, "rss: limit 8192→9420", results)
    check("6594" in wiki, "rss: calculated -Xmx grows with limit", results)
    check("maxgcpausemillis=200" not in lead, "rss: does not copy G1 pause flag", results)
    check("histogram" not in lead, "rss: healthy GC does not ask for dump", results)


def test_gc_p95_user_latency(results: list[tuple[bool, str]]) -> None:
    """Паузы GC выше порога и бьют по пользователю: копировать G1, квоты не трогать."""
    code, wiki, err = _run(
        "gc_p95_user_latency",
        JvmAnalyzeRequest(
            system_name="DEMO_CounterAgent",
            container_name="application",
            gc_pause_p95_ms=320.0,
            tree=_base_tree(user_latency_grew="yes"),
        ),
    )
    lead = _lead(wiki)
    res = _resources(wiki)
    check(code == 0, f"gc_p95 runs ({err})", results)
    check("h2. выкатывать java_tool_options\n*да*" in lead, "gc_p95: copyable allowed", results)
    check("javatooloptions: >" in wiki.lower(), "gc_p95: copyable jvm-config present", results)
    check("| -XX:MaxGCPauseMillis | 250 | 200 |" in res, "gc_p95: pause 250→200", results)
    check(
        "| -XX:InitiatingHeapOccupancyPercent | не задан | 30 |" in res
        or "| -XX:InitiatingHeapOccupancyPercent | не задан | 30 |" in wiki,
        "gc_p95: IHOP → 30 with purpose column nearby",
        results,
    )
    check("что делает" in res.lower(), "gc_p95: flag table explains the parameter", results)
    check("| Memory request (MiB) |" not in res, "gc_p95: does not change memory request", results)
    check("| Memory limit (MiB) |" not in res, "gc_p95: does not change memory limit", results)


def test_ha_no_headroom(results: list[tuple[bool, str]]) -> None:
    """Сумма CPU плеч > 80%: ёмкость платформы, не G1."""
    code, wiki, err = _run(
        "ha_no_headroom",
        JvmAnalyzeRequest(
            system_name="DEMO_CounterAgent",
            container_name="application",
            gc_pause_p95_ms=320.0,
            tree=_base_tree(
                cpu_throttled="yes",
                cpu_pct_limits_shoulder_1=50.0,
                cpu_pct_limits_shoulder_2=40.0,
                user_latency_grew="yes",
            ),
        ),
    )
    lead = _lead(wiki)
    check(code == 0, f"ha runs ({err})", results)
    check("javatooloptions: >" not in lead, "ha: no copyable jvm-config", results)
    check("нет запаса на отказ плеча" in lead, "ha: names shoulder failover", results)
    check("добавьте cpu limit или поды" in lead, "ha: says add CPU or pods", results)
    check("maxgcpausemillis=200" not in lead, "ha: does not tell to copy G1", results)


def test_oldgen_live_objects(results: list[tuple[bool, str]]) -> None:
    """OldGen после GC не снизился: dump/histogram разработке, не G1."""
    code, wiki, err = _run(
        "oldgen_live_objects",
        JvmAnalyzeRequest(
            system_name="DEMO_CounterAgent",
            container_name="application",
            gc_pause_p95_ms=80.0,
            old_gen_used_percent=75.0,
            old_gen_capacity_mib=4096,
            tree=_base_tree(
                heap_growing="yes",
                growth_of="oldgen",
                heap_growth_percent=10.0,
                heap_growth_hours=12.0,
                gc_ran_in_window="yes",
                heap_used_before_gc_mib=4000,
                heap_used_after_gc_mib=3800,
                oldgen_returned_after_gc="no",
            ),
        ),
    )
    lead = _lead(wiki)
    check(code == 0, f"oldgen_live runs ({err})", results)
    check("javatooloptions: >" not in lead, "oldgen_live: no copyable jvm-config", results)
    check("histogram" in lead, "oldgen_live: asks for histogram or dump", results)
    check("топ классов" in lead, "oldgen_live: says what to look at", results)
    check("разработке" in lead, "oldgen_live: next step is developers", results)
    check("maxgcpausemillis=200" not in lead, "oldgen_live: does not copy G1 pause", results)
    check("memory limit" in lead, "oldgen_live: still raises limit to buy time before OOM", results)


def test_evicted(results: list[tuple[bool, str]]) -> None:
    """Evicted: нода/квота, не JVM."""
    code, wiki, err = _run(
        "evicted",
        JvmAnalyzeRequest(
            system_name="DEMO_CounterAgent",
            container_name="application",
            gc_pause_p95_ms=80.0,
            tree=_base_tree(restart_kind="evicted"),
        ),
    )
    lead = _lead(wiki)
    check(code == 0, f"evicted runs ({err})", results)
    check("javatooloptions: >" not in lead, "evicted: no copyable jvm-config", results)
    check("disk-pressure" in lead or "квот" in lead, "evicted: points to node/quota", results)
    check("maxgcpausemillis=200" not in lead, "evicted: does not copy G1", results)


def test_oom_cause_open(results: list[tuple[bool, str]]) -> None:
    """OOMKilled, причина не закрыта: флаги не копировать."""
    code, wiki, err = _run(
        "oom_cause_open",
        JvmAnalyzeRequest(
            system_name="DEMO_CounterAgent",
            container_name="application",
            gc_pause_p95_ms=320.0,
            tree=_base_tree(
                restart_kind="oomkilled",
                memory_cause_closed="no",
                user_latency_grew="yes",
            ),
        ),
    )
    lead = _lead(wiki)
    check(code == 0, f"oom_open runs ({err})", results)
    check("javatooloptions: >" not in lead, "oom_open: no copyable jvm-config", results)
    check("oomkilled" in lead or "причина по памяти" in lead, "oom_open: says memory cause is still open", results)


def test_oom_cause_closed_gc_pauses(results: list[tuple[bool, str]]) -> None:
    """OOM закрыт, паузы бьют по пользователю: G1 можно копировать."""
    code, wiki, err = _run(
        "oom_cause_closed",
        JvmAnalyzeRequest(
            system_name="DEMO_CounterAgent",
            container_name="application",
            gc_pause_p95_ms=320.0,
            tree=_base_tree(
                restart_kind="oomkilled",
                memory_cause_closed="yes",
                user_latency_grew="yes",
            ),
        ),
    )
    lead = _lead(wiki)
    check(code == 0, f"oom_closed runs ({err})", results)
    check("javatooloptions: >" in wiki.lower(), "oom_closed: copyable jvm-config present", results)
    check("*да*" in lead.split("h2. что сделать", 1)[0], "oom_closed: copyable allowed", results)


def test_request_pressure_only(results: list[tuple[bool, str]]) -> None:
    """Working set выше request, память не растёт: поднять request, G1 не трогать."""
    code, wiki, err = _run(
        "request_pressure",
        JvmAnalyzeRequest(
            system_name="DEMO_CounterAgent",
            container_name="application",
            gc_pause_p95_ms=80.0,
            container_memory_working_set_mib=5200,
            tree=_base_tree(),
        ),
    )
    res = _resources(wiki)
    check(code == 0, f"request_pressure runs ({err})", results)
    check(
        "| Memory request (MiB) | 4096 | 5200 |" in res,
        "request_pressure: table shows 4096→5200",
        results,
    )
    check("| Memory limit (MiB) |" not in res, "request_pressure: does not change limit", results)
    check("javatooloptions: >" not in _lead(wiki), "request_pressure: no G1 copy (p95 below threshold)", results)


def test_gc_throttle_cpu(results: list[tuple[bool, str]]) -> None:
    """Долгие паузы совпадают с throttle: поднимать CPU limit, не G1."""
    code, wiki, err = _run(
        "gc_throttle_cpu",
        JvmAnalyzeRequest(
            system_name="DEMO_CounterAgent",
            container_name="application",
            gc_pause_p95_ms=400.0,
            tree=_base_tree(
                cpu_throttled="yes",
                cpu_pct_limits_shoulder_1=35.0,
                cpu_pct_limits_shoulder_2=30.0,
                user_latency_grew="yes",
                pauses_coincide_throttle="yes",
            ),
        ),
    )
    lead = _lead(wiki)
    check(code == 0, f"gc_throttle_cpu runs ({err})", results)
    check("javatooloptions: >" not in lead, "gc_throttle_cpu: no copyable jvm-config", results)
    check("cpu limit" in lead, "gc_throttle_cpu: action raises CPU limit", results)
    check("maxgcpausemillis=200" not in lead, "gc_throttle_cpu: does not copy G1 pause flag", results)
    check("| Memory request (MiB) |" not in _resources(wiki), "gc_throttle_cpu: does not change memory request", results)
    check("| Memory limit (MiB) |" not in _resources(wiki), "gc_throttle_cpu: does not change memory limit", results)


def test_slow_rss_1pct_12h(results: list[tuple[bool, str]]) -> None:
    """1% RSS за 12 ч при usage 15%: до SLA 80% > 30 дней, не критично."""
    code, wiki, err = _run(
        "slow_rss_1pct_12h",
        JvmAnalyzeRequest(
            system_name="DEMO_CounterAgent",
            container_name="application",
            gc_pause_p95_ms=80.0,
            container_memory_usage_percent=15.0,
            tree=_base_tree(
                heap_growing="yes",
                growth_of="container_rss",
                heap_growth_percent=1.0,
                heap_growth_hours=12.0,
                gc_ran_in_window="yes",
                heap_used_before_gc_mib=600,
                heap_used_after_gc_mib=400,
                oldgen_returned_after_gc="yes",
            ),
        ),
    )
    lead = _lead(wiki)
    res = _resources(wiki)
    check(code == 0, f"slow_rss_1pct_12h runs ({err})", results)
    check("не критично" in lead, "slow_rss: action says not critical", results)
    check("30 дн" in lead or "30 дней" in lead, "slow_rss: names 30-day SLA horizon", results)
    check("увеличьте memory request" not in lead, "slow_rss: does not raise request", results)
    check("увеличьте memory limit" not in lead, "slow_rss: does not raise limit", results)
    check("| Memory limit (MiB) | 8192 | 9420 |" not in res, "slow_rss: no limit 8192→9420", results)
    check("| Memory request (MiB) |" not in res, "slow_rss: resources table does not change request", results)
    check("maxgcpausemillis=200" not in lead, "slow_rss: does not copy G1 pause flag", results)


def test_throttle_ha_ok(results: list[tuple[bool, str]]) -> None:
    """Throttle есть, но 70+10=80: копировать G1 можно."""
    code, wiki, err = _run(
        "throttle_ha_ok",
        JvmAnalyzeRequest(
            system_name="DEMO_CounterAgent",
            container_name="application",
            gc_pause_p95_ms=320.0,
            tree=_base_tree(
                cpu_throttled="yes",
                cpu_pct_limits_shoulder_1=70.0,
                cpu_pct_limits_shoulder_2=10.0,
                user_latency_grew="yes",
            ),
        ),
    )
    lead = _lead(wiki)
    check(code == 0, f"throttle_ha_ok runs ({err})", results)
    check("javatooloptions: >" in wiki.lower(), "throttle_ha_ok: copyable jvm-config present", results)
    check("нет запаса на отказ плеча" not in lead, "throttle_ha_ok: 80 is not a breach", results)


def test_gc_long_no_symptoms(results: list[tuple[bool, str]]) -> None:
    """Длинные паузы (20 с) без user latency: доп. анализ, не копировать G1."""
    code, wiki, err = _run(
        "gc_long_no_symptoms",
        JvmAnalyzeRequest(
            system_name="DEMO_CounterAgent",
            container_name="application",
            gc_pause_p95_ms=20000.0,
            tree=_base_tree(user_latency_grew="no"),
        ),
    )
    lead = _lead(wiki)
    check(code == 0, f"gc_long_no_symptoms runs ({err})", results)
    check("javatooloptions: >" not in lead, "gc_long_no_symptoms: no copyable jvm-config", results)
    check("gc log" in lead.lower() and "safepoint" in lead.lower(), "gc_long_no_symptoms: extra analysis tools", results)
    check("liveness" in lead.lower() or "heartbeat" in lead.lower(), "gc_long_no_symptoms: when it is critical", results)
    check("maxgcpausemillis=200" not in lead, "gc_long_no_symptoms: does not copy G1 pause flag", results)


def test_gc_six_minute_pause(results: list[tuple[bool, str]]) -> None:
    """Пауза 6 минут без симптомов: тот же путь, плюс пометка extreme."""
    code, wiki, err = _run(
        "gc_six_minute_pause",
        JvmAnalyzeRequest(
            system_name="DEMO_CounterAgent",
            container_name="application",
            gc_pause_p95_ms=360000.0,
            tree=_base_tree(user_latency_grew="no"),
        ),
    )
    lead = _lead(wiki)
    check(code == 0, f"gc_six_minute_pause runs ({err})", results)
    check("javatooloptions: >" not in lead, "six-minute: no copyable jvm-config", results)
    check("обслуживает пользователей" in lead or "stop-the-world" in lead, "six-minute: serving-pod incident hint", results)
    check("maxgcpausemillis=200" not in lead, "six-minute: does not copy G1", results)


def test_rising_gc_floor(results: list[tuple[bool, str]]) -> None:
    """Очистка есть, но пол после GC растёт: проблема всё ещё есть."""
    code, wiki, err = _run(
        "rising_gc_floor",
        JvmAnalyzeRequest(
            system_name="DEMO_CounterAgent",
            container_name="application",
            gc_pause_p95_ms=80.0,
            tree=_base_tree(
                heap_growing="yes",
                growth_of="heap",
                heap_growth_percent=8.0,
                heap_growth_hours=24.0,
                gc_ran_in_window="yes",
                heap_used_before_gc_mib=4000,
                heap_used_after_gc_mib=1800,
                post_gc_floor_rising="yes",
            ),
        ),
    )
    lead = _lead(wiki)
    check(code == 0, f"rising_gc_floor runs ({err})", results)
    check("проблема всё ещё есть" in lead or "проблема все еще есть" in lead, "rising_gc_floor: problem remains", results)
    check("это не утечка" not in lead, "rising_gc_floor: does not call it a non-leak", results)
    check("histogram" in lead.lower(), "rising_gc_floor: histogram / dump", results)
    check("maxgcpausemillis=200" not in lead, "rising_gc_floor: does not copy G1", results)
    check("не со старта пода" in lead, "rising_gc_floor: rate is not from pod start", results)


def test_gc_cleanup_cpu_spike(results: list[tuple[bool, str]]) -> None:
    """Большая очистка бьёт по CPU SLA: не поднимать -Xmx, не копировать G1."""
    code, wiki, err = _run(
        "gc_cleanup_cpu_spike",
        JvmAnalyzeRequest(
            system_name="DEMO_CounterAgent",
            container_name="application",
            gc_pause_p95_ms=400.0,
            tree=_base_tree(
                heap_growing="yes",
                growth_of="heap",
                gc_ran_in_window="yes",
                heap_used_before_gc_mib=5000,
                heap_used_after_gc_mib=1200,
                gc_cpu_spike_sla="yes",
                user_latency_grew="yes",
            ),
        ),
    )
    lead = _lead(wiki)
    check(code == 0, f"gc_cleanup_cpu_spike runs ({err})", results)
    check("javatooloptions: >" not in lead, "cpu_spike: no copyable jvm-config", results)
    check("cpu" in lead.lower() and "sla" in lead.lower(), "cpu_spike: names CPU SLA", results)
    check("-xmx" in lead.lower() and "не поднимайте" in lead.lower(), "cpu_spike: do not raise -Xmx", results)
    check("maxgcpausemillis=200" not in lead, "cpu_spike: does not copy G1 pause flag", results)


def test_growth_below_sla_plan(results: list[tuple[bool, str]]) -> None:
    """Рост есть, SLA ещё нет: план SLA/OOM/плато и предупреждение про рестарт."""
    code, wiki, err = _run(
        "growth_below_sla_plan",
        JvmAnalyzeRequest(
            system_name="DEMO_CounterAgent",
            container_name="application",
            gc_pause_p95_ms=80.0,
            container_memory_usage_percent=50.0,
            tree=_base_tree(
                heap_growing="yes",
                growth_of="container_rss",
                heap_growth_percent=2.0,
                heap_growth_hours=24.0,
            ),
        ),
    )
    lead = _lead(wiki)
    check(code == 0, f"growth_below_sla_plan runs ({err})", results)
    check("плато" in lead, "growth_plan: mentions plateau", results)
    check("75%" in lead or "75 %" in lead, "growth_plan: example plateau 75%", results)
    check("не со старта пода" in lead, "growth_plan: not from pod start", results)
    check("перезапустит под" in lead, "growth_plan: settings change restarts the pod", results)
    check("ретест" in lead.lower(), "growth_plan: retest after restart is not comparable", results)


def test_java_oome_open(results: list[tuple[bool, str]]) -> None:
    """Java OOME, причина не закрыта: как OOMKilled, флаги не копировать."""
    code, wiki, err = _run(
        "java_oome_open",
        JvmAnalyzeRequest(
            system_name="DEMO_CounterAgent",
            container_name="application",
            gc_pause_p95_ms=80.0,
            tree=_base_tree(restart_kind="java_oome", memory_cause_closed="no"),
        ),
    )
    lead = _lead(wiki)
    check(code == 0, f"java_oome_open runs ({err})", results)
    check("javatooloptions: >" not in lead, "java_oome: no copyable jvm-config", results)
    check("outofmemoryerror" in lead or "oome" in lead, "java_oome: names Java OOME", results)
    check("maxgcpausemillis=200" not in lead, "java_oome: does not copy G1", results)


def test_gc_not_ran_yet(results: list[tuple[bool, str]]) -> None:
    """Рост есть, GC ещё не отработал: это не утечка."""
    code, wiki, err = _run(
        "gc_not_ran_yet",
        JvmAnalyzeRequest(
            system_name="DEMO_CounterAgent",
            container_name="application",
            gc_pause_p95_ms=80.0,
            tree=_base_tree(
                heap_growing="yes",
                growth_of="heap",
                heap_growth_percent=10.0,
                heap_growth_hours=12.0,
                gc_ran_in_window="no",
            ),
        ),
    )
    lead = _lead(wiki)
    check(code == 0, f"gc_not_ran_yet runs ({err})", results)
    check("ещё не утечка" in lead or "еще не утечка" in lead, "gc_not_ran: not a leak yet", results)
    check("ещё через 12 ч" in lead or "еще через 12 ч" in lead, "gc_not_ran: second 12h window", results)
    check("maxgcpausemillis=200" not in lead, "gc_not_ran: does not copy G1", results)


def test_rss_steep_gc_pending(results: list[tuple[bool, str]]) -> None:
    """RSS +10% за 12 ч, GC ещё нет, usage низкий: второй замер, limit не поднимать."""
    code, wiki, err = _run(
        "rss_steep_gc_pending",
        JvmAnalyzeRequest(
            system_name="DEMO_CounterAgent",
            container_name="application",
            gc_pause_p95_ms=80.0,
            container_memory_usage_percent=15.0,
            tree=_base_tree(
                pods_per_shoulder=1,
                heap_growing="yes",
                growth_of="container_rss",
                heap_growth_percent=10.0,
                heap_growth_hours=12.0,
                gc_ran_in_window="no",
            ),
        ),
    )
    lead = _lead(wiki)
    check(code == 0, f"rss_steep_gc_pending runs ({err})", results)
    check("javatooloptions: >" not in lead, "rss_pending: no copyable jvm-config", results)
    check("ещё не утечка" in lead or "еще не утечка" in lead, "rss_pending: not a leak yet", results)
    check("ещё через 12 ч" in lead or "еще через 12 ч" in lead, "rss_pending: watch another 12h", results)
    check("не поднимайте" in lead or "не трогайте" in lead, "rss_pending: do not raise limit yet", results)
    check("прогрев или кэш" in lead, "rss_pending: rate may be warmup/cache", results)
    check("maxgcpausemillis=200" not in lead, "rss_pending: does not copy G1", results)
    check("тюнинг уже пробовали" not in wiki.lower(), "rss_pending: no fake lifecycle status", results)


def test_pauses_coincide_no(results: list[tuple[bool, str]]) -> None:
    """Throttle есть, паузы с ним не совпадают: G1 можно копировать."""
    code, wiki, err = _run(
        "pauses_coincide_no",
        JvmAnalyzeRequest(
            system_name="DEMO_CounterAgent",
            container_name="application",
            gc_pause_p95_ms=320.0,
            tree=_base_tree(
                cpu_throttled="yes",
                cpu_pct_limits_shoulder_1=35.0,
                cpu_pct_limits_shoulder_2=30.0,
                user_latency_grew="yes",
                pauses_coincide_throttle="no",
            ),
        ),
    )
    lead = _lead(wiki)
    check(code == 0, f"pauses_coincide_no runs ({err})", results)
    check("javatooloptions: >" in wiki.lower(), "coincide_no: copyable jvm-config present", results)
    check("поднимите cpu limit" not in lead, "coincide_no: does not treat as CPU steal", results)
    check("| -XX:MaxGCPauseMillis | 250 | 200 |" in _resources(wiki), "coincide_no: pause 250→200", results)


def test_sla_already_80(results: list[tuple[bool, str]]) -> None:
    """Usage уже 82%: SLA 80% нарушен, 30-дневный горизонт не спасает."""
    code, wiki, err = _run(
        "sla_already_80",
        JvmAnalyzeRequest(
            system_name="DEMO_CounterAgent",
            container_name="application",
            gc_pause_p95_ms=80.0,
            container_memory_usage_percent=82.0,
            tree=_base_tree(
                heap_growing="yes",
                growth_of="container_rss",
                heap_growth_percent=1.0,
                heap_growth_hours=24.0,
            ),
        ),
    )
    lead = _lead(wiki)
    check(code == 0, f"sla_already_80 runs ({err})", results)
    check("уже на уровне sla 80%" in lead, "sla_already_80: already at SLA", results)
    check("не критично" not in lead.split("h3. план", 1)[0], "sla_already_80: action is not the 30-day not-critical path", results)


def test_user_latency_unknown(results: list[tuple[bool, str]]) -> None:
    """p95 выше порога, влияние на пользователя неизвестно: паузы не копировать."""
    code, wiki, err = _run(
        "user_latency_unknown",
        JvmAnalyzeRequest(
            system_name="DEMO_CounterAgent",
            container_name="application",
            gc_pause_p95_ms=320.0,
            tree=_base_tree(user_latency_grew="unknown"),
        ),
    )
    lead = _lead(wiki)
    check(code == 0, f"user_latency_unknown runs ({err})", results)
    check("javatooloptions: >" not in lead, "latency_unknown: no copyable jvm-config", results)
    check("не ясно" in lead, "latency_unknown: says user impact is unknown", results)
    check("maxgcpausemillis=200" not in lead, "latency_unknown: does not copy G1 pause flag", results)


def test_post_gc_floor_stable(results: list[tuple[bool, str]]) -> None:
    """Очистка есть, пол после GC не растёт: это не утечка."""
    code, wiki, err = _run(
        "post_gc_floor_stable",
        JvmAnalyzeRequest(
            system_name="DEMO_CounterAgent",
            container_name="application",
            gc_pause_p95_ms=80.0,
            tree=_base_tree(
                heap_growing="yes",
                growth_of="heap",
                gc_ran_in_window="yes",
                heap_used_before_gc_mib=4000,
                heap_used_after_gc_mib=1200,
                post_gc_floor_rising="no",
            ),
        ),
    )
    lead = _lead(wiki)
    check(code == 0, f"post_gc_floor_stable runs ({err})", results)
    check("это не утечка" in lead, "floor_stable: says not a leak", results)
    check("-xmx" in lead and "не поднимайте" in lead, "floor_stable: do not raise -Xmx", results)
    check("maxgcpausemillis=200" not in lead, "floor_stable: does not copy G1", results)


def test_throttle_cpu_missing(results: list[tuple[bool, str]]) -> None:
    """Throttle есть, CPU % of limits не заполнены: флаги не копировать."""
    code, wiki, err = _run(
        "throttle_cpu_missing",
        JvmAnalyzeRequest(
            system_name="DEMO_CounterAgent",
            container_name="application",
            gc_pause_p95_ms=320.0,
            tree=_base_tree(cpu_throttled="yes", user_latency_grew="yes"),
        ),
    )
    lead = _lead(wiki)
    check(code == 0, f"throttle_cpu_missing runs ({err})", results)
    check("javatooloptions: >" not in lead, "cpu_missing: no copyable jvm-config", results)
    check("cpu % of limits" in lead, "cpu_missing: names the missing metric", results)
    check("maxgcpausemillis=200" not in lead, "cpu_missing: does not copy G1", results)


def test_wiki_copy_hygiene(results: list[tuple[bool, str]]) -> None:
    """Сгенерированные wiki без калек и обрывков, которые ломают чтение."""
    forbidden = (
        "знаменатель",
        "cpu burst",
        "serving-под",
        "serving-поде",
        "user latency",
        "не доказанная боль",
        "*рестарт:* none",
        "*рестарт:* java_oome",
        "warmup и заполнение",
        "warmup и кэш",
        "тюнинг уже пробовали",
    )
    wikis = list(OUT.glob("*/jvm_confluence.wiki"))
    check(len(wikis) >= 10, f"hygiene: generated wiki files exist ({len(wikis)})", results)
    blob = "\n".join(path.read_text(encoding="utf-8") for path in wikis).lower()
    for phrase in forbidden:
        check(phrase not in blob, f"hygiene: wiki has no «{phrase}»", results)


def test_integer_xmx_resize(results: list[tuple[bool, str]]) -> None:
    """Целый -Xmx масштабируется вместе с limit."""
    resize = propose_memory_heap_resize(
        limit_mib=8192,
        request_mib=4096,
        java_tool_options=["-Xmx2048m"],
        need_resize=True,
    )
    check(resize.resized, "xmx: resize emitted", results)
    check(resize.limit_to == 9420 and resize.request_to == 5324, "xmx: same quota step as percent mode", results)
    check(resize.xmx_was_mib == 2048 and resize.xmx_to_mib == 2355, "xmx: 2048m scales to 2355m", results)


def main() -> int:
    results: list[tuple[bool, str]] = []
    test_rss_gc_healthy(results)
    test_gc_p95_user_latency(results)
    test_ha_no_headroom(results)
    test_oldgen_live_objects(results)
    test_evicted(results)
    test_oom_cause_open(results)
    test_oom_cause_closed_gc_pauses(results)
    test_request_pressure_only(results)
    test_throttle_ha_ok(results)
    test_gc_throttle_cpu(results)
    test_slow_rss_1pct_12h(results)
    test_gc_long_no_symptoms(results)
    test_gc_six_minute_pause(results)
    test_rising_gc_floor(results)
    test_gc_cleanup_cpu_spike(results)
    test_growth_below_sla_plan(results)
    test_java_oome_open(results)
    test_gc_not_ran_yet(results)
    test_rss_steep_gc_pending(results)
    test_pauses_coincide_no(results)
    test_sla_already_80(results)
    test_user_latency_unknown(results)
    test_post_gc_floor_stable(results)
    test_throttle_cpu_missing(results)
    test_wiki_copy_hygiene(results)
    test_integer_xmx_resize(results)
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
