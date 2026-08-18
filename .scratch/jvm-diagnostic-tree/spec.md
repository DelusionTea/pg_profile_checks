Status: ready-for-agent
Type: enhancement

# JVM diagnostic tree (cutoff + copyable flag gate)

## Problem Statement

Colleagues run JVM checks, see a copy-paste `JAVA_TOOL_OPTIONS` block, and apply it without checking whether the pain is GC, CPU throttle, an OOM/Evicted restart, or missing HA headroom across two Kubernetes clusters («плечи»). The UI currently forces three metrics, offers problem checkboxes/presets, and seeds findings from those checkboxes even when no number was entered. There is no throttle/HA analyser in the engine today.

## Solution

A sequential cutoff tree in the JVM UI (playbook yaml, no live Qwen in this slice). Dialogue answers do not create findings unless a number is present or existing yaml/config parse produced them. `jvmcheck` still runs once at the end. The Confluence wiki leads with whether a copyable `JAVA_TOOL_OPTIONS` string is allowed; if not, flag candidates stay in Expand as «кандидат / зачем / что снять». Platform HA headroom (sum of this AS’s CPU % of limits on both shoulders > 80) is a platform finding, not a G1 flag.

## User Stories

1. As an on-call engineer, I want to pick AS / pod / container first, so that analysis targets the right workload.
2. As an on-call engineer, I want to enter how many pods sit on one shoulder, so that the wiki records replica count without asking how many shoulders exist.
3. As an on-call engineer, I want the tool to assume there are always two fully duplicated Kubernetes clusters (shoulders), so that I am not asked a meaningless cluster-count question.
4. As an on-call engineer, I want restart cause asked as OOMKilled / Evicted / Java OOME / none / unknown separately, so that node eviction is not treated as a Java heap problem.
5. As an on-call engineer, I want «не знаю» to be a valid answer everywhere, so that I can still run analysis without inventing numbers.
6. As an on-call engineer, I want analysis to run even when answers are unknown, so that config-audit findings from yaml still appear.
7. As an on-call engineer, I want the tool never to say «apply this flag» when a critical node is unknown, so that I am not pushed into a blind rollout.
8. As an on-call engineer, I want to be told what to re-check when I answered unknown, so that I know the next measurement.
9. As an on-call engineer who answered OOMKilled or Java OOME, I want a follow-up «причина памяти закрыта?» and heap/OldGen fields, so that heap questions appear only on the memory path.
10. As an on-call engineer with an open OOM/OOME, I want no copyable `JAVA_TOOL_OPTIONS` until I explicitly say the memory cause is closed, so that G1 flags are not used as an OOM workaround.
11. As an on-call engineer who answered Evicted, I want a platform finding and no copyable JVM flags, so that node pressure is not treated as GC tuning.
12. As an on-call engineer, I want to answer whether CPU throttle is happening, so that the tree can separate platform CPU from GC pauses.
13. As an on-call engineer who answered throttle=yes, I want to enter CPU % of limits for this AS on shoulder 1 and shoulder 2, so that HA headroom can be judged.
14. As an on-call engineer, I want the denominator of those percentages to be the sum of this AS’s container CPU limits on that cluster (not node CPU, not request, not JVM User/Sys), so that the number matches Kubernetes throttle semantics.
15. As an on-call engineer, I want a platform finding «нет запаса на отказ плеча» when the two-shoulder sum is greater than 80, so that the team scales capacity instead of shipping G1 flags.
16. As an on-call engineer, I want missing shoulder CPU numbers (when throttle=yes) to keep throttle criticality unknown and to block the copyable string, so that incomplete Grafana pastes cannot downgrade the gate.
17. As an on-call engineer with sticky sessions, I want 70% + 10% (sum 80) treated as acceptable HA headroom, so that uneven cluster traffic is not diagnosed as a balancer bug.
18. As an on-call engineer, I never want the wiki to say «почините баланс между кластерами», so that sticky-session AS are not given a false action.
19. As an on-call engineer, I want GC p95 asked after the throttle/HA node, so that pause analysis is not the first wall of fields.
20. As an on-call engineer, I want GC p95 compared to the existing yaml default threshold (250 ms on the normal profile), so that we do not invent a new pause SLA.
21. As an on-call engineer, I want user-facing latency asked only when GC p95 is present and above that threshold, so that the first screen stays small.
22. As an on-call engineer, I want user latency to be yes / no / unknown with an optional number, so that we can record impact without forcing a second Grafana scrape.
23. As an on-call engineer, I want unknown user-latency impact to mean we do not claim pauses caused user pain and we do not emit a copyable pause string, so that correlation is not invented.
24. As an on-call engineer with throttle=yes and GC analysis required, I want to be asked whether pauses coincided with throttle, so that CPU steal/throttle is not ignored when looking at p95.
25. As an on-call engineer, I want no throughput / p99 / GC-time-ratio node in this tree, so that the UI stays at the agreed minimum.
26. As an on-call engineer, I want no problem checkboxes and no GC/Heap/Memory presets, so that I cannot skip the cutoff tree.
27. As an on-call engineer, I want the three former always-required fields (GC p95, heap used, memory %) to be optional and contextual, so that the form does not scare people who only have part of Grafana.
28. As an on-call engineer, I want `jvmcheck` to run once after the tree, so that yaml flag audit still happens.
29. As an on-call engineer, I want dialogue answers never to seed `gc.long_pause_p95` (or similar) without a number, so that ticking a story is not treated as evidence.
30. As an on-call engineer, I want a copyable `JAVA_TOOL_OPTIONS` / jvm-config yaml block only when every critical node is closed and a confirming metric exists, so that I cannot copy a string in the unknown/HA/OOM cases.
31. As an on-call engineer blocked from copying, I want an Expand table of flag candidates with why and what to remove, so that I still see options without a ready-to-ship line.
32. As an on-call engineer, I want each recommendation in Confluence to carry «На основании», «Перепроверьте», and the dialogue answers, so that a mixed audience can audit the path.
33. As an on-call engineer, I want the same Confluence wiki as the deliverable (not a chat product), so that the page can be pasted as today.
34. As a reviewer, I want live Qwen out of this slice, so that the tree ships while the LLM probe is `dry_run`.
35. As a reviewer, I want packing-on-the-same-node, RAG, dumps/JFR, `-Xmn`, and manual IHOP out of this slice, so that v1 stays the cutoff tree plus gates.

## Implementation Decisions

- One deep module owns the tree: load playbook yaml, evaluate answers, decide copyable/pause-copyable, emit platform findings, format the Russian wiki lead-in and the candidates Expand. UI and `jvm_runner` are adapters.
- Playbook yaml holds: always two shoulders, HA sum limit 80, question ids/labels, and the GC p95 threshold used for «analysis required» (read from existing JVM thresholds default 250 ms; do not add a second pause SLA).
- `jvmcheck` health analyser is unchanged: it still does not invent a throttle or cluster-packing detector. HA/throttle criticality is only from form answers.
- Request payload grows a `tree` object (pods per shoulder, restart kind, memory-cause-closed, throttle, two CPU %, user latency, pauses-coincide). Existing metric fields stay; heap metrics are passed into `jvmcheck` only when the heap branch is open.
- Remove always-required metric 400s, selected-problem seeding, selected-problem filtering as a substitute for the tree, and UI presets/checkboxes.
- When the master copyable gate is closed, strip `suggested_java_tool_options` from recommendations before building the copy/paste yaml block; keep the flags as candidates for Expand.
- When the master gate is open but pause-copyable is closed, strip flags only from pause-rule recommendations (`gc.long_pause_p95` and related pause rules).
- Do not emit balancer/sticky remediation text. Do not emit «примените этот флаг» when the copyable gate is closed.
- Qwen, RAG, p99/throughput nodes, and intra-shoulder 40% packing are not implemented.

## Testing Decisions

Tests observe external behaviour at one primary seam: evaluate answers → gate + wiki lead-in (+ one adapter run through `run_jvm_analysis` on a demo AS).

Good tests assert user-visible outcomes with literals from this spec (80, two shoulders, 70+10 is allowed, 50+40 is not, unknown blocks copyable, OOM without closed memory blocks copyable, empty p95 blocks copyable, 70+10 wiki has no balancer sentence, checkbox-seed codes do not appear without a number). They do not assert private helper names.

Prior art: `scripts/check_*.py` (see `scripts/check_confluence_ux.py`), invoked from `scripts/check_smoke.py`.

Seams:

1. **Primary:** `evaluate_jvm_diagnostic_tree` / `apply_tree_gates` / tree wiki formatter (public functions of the diagnostic-tree module).
2. **Adapter:** `run_jvm_analysis` with tree answers on `DEMO_CounterAgent` — wiki has/hasn't a copyable `javaToolOptions` block according to the gate; no `gc.long_pause_p95` without a p95 number.
3. **UI contract:** JVM HTML/JS has the tree questions and does not contain preset buttons or the old «обязательные поля: GC p95, Heap used, Memory usage» copy.

## Out of Scope

Live Qwen on JVM, RAG, same-node packing 40%, sticky balancer repair, throughput/p99/GC time ratio tree nodes, dumps/JFR/`-Xmn`/manual IHOP, rewriting NT/PROD pg_profile wiki.

## Further Notes

«Плечо» is one of two fully duplicated Kubernetes clusters used for HA if an entire cluster dies. CPU % of limits is aggregated for this AS on that cluster, not per pod. 80% is operator HA policy in playbook yaml, not a Kubernetes built-in. Default GC p95 threshold remains `thresholds_jvm.yaml` defaults (250 ms).
