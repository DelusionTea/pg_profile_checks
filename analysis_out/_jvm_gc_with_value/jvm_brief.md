# JVM Analysis Brief

- System: `DEMO_CounterAgent`
- Container: `application`
- Selected problems: `gc_latency`
- Threshold profile: `normal`
- Findings: `3`

## [HIGH] gc.long_pause_p95
- Message: GC p95 pause exceeds threshold.
- Threshold: gc_pause_p95_ms > 250.0

## [INFO] heap.metric_missing
- Message: Heap utilization is not provided. Recommendations may be less precise.
- Threshold: provide heap_used_mib or heap_used_percent

## [INFO] oldgen.metric_missing
- Message: OldGen utilization is not provided. OldGen recommendations may be less precise.
- Threshold: provide old_gen_used_percent or old_gen_used_mib

## Problem input coverage
- gc_latency: status=partial, missing_metrics=GC pause p99, GC time ratio
