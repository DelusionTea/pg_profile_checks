# JVM Analysis Brief

- System: `DEMO_CounterAgent`
- Container: `application`
- Selected problems: `gc_latency`
- Threshold profile: `normal`
- Findings: `1`

## [WARNING] gc.long_pause_p95
- Message: Выбрана проблема долгих GC пауз, требуется tuning tail latency.
- Threshold: selected by operator

## Problem input coverage
- gc_latency: status=partial, missing_metrics=GC pause p95, GC pause p99, GC time ratio

## Guardrails
- GC latency observed with already high heap utilization: avoid heap shrinking and prioritize GC policy tuning.
