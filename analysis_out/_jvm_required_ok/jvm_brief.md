# JVM Analysis Brief

- System: `DEMO_CounterAgent`
- Container: `application`
- Selected problems: `gc_latency`
- Threshold profile: `normal`
- Findings: `3`

## [HIGH] gc.long_pause_p95
- Message: GC p95 pause exceeds threshold.
- Threshold: gc_pause_p95_ms > 250.0

## [HIGH] gc.long_pause_p99
- Message: GC p99 pause exceeds threshold.
- Threshold: gc_pause_p99_ms > 400.0

## [INFO] oldgen.metric_missing
- Message: OldGen utilization is not provided. OldGen recommendations may be less precise.
- Threshold: provide old_gen_used_percent or old_gen_used_mib

## Problem input coverage
- gc_latency: status=ok, missing_metrics=-
