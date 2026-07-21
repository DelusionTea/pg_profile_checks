# JVM Analysis Brief

- System: `DEMO_CounterAgent`
- Container: `application`
- Selected problems: `gc_latency, heap_pressure`
- Threshold profile: `normal`
- Findings: `4`

## [HIGH] gc.long_pause_p95
- Message: GC p95 pause exceeds threshold.
- Threshold: gc_pause_p95_ms > 250.0

## [HIGH] gc.long_pause_p99
- Message: GC p99 pause exceeds threshold.
- Threshold: gc_pause_p99_ms > 400.0

## [HIGH] gc.high_time_ratio
- Message: GC time ratio exceeds threshold.
- Threshold: gc_time_ratio_percent > 15.0

## [WARNING] heap.old_gen_pressure
- Message: Выбрано давление по heap/old gen, требуется стабилизация occupancy.
- Threshold: selected by operator

## Problem input coverage
- gc_latency: status=ok, missing_metrics=-
- heap_pressure: status=ok, missing_metrics=-
