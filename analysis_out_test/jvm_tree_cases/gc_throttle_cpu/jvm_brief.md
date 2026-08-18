# JVM Analysis Brief

- System: `DEMO_CounterAgent`
- Container: `application`
- Selected problems: ``
- Threshold profile: `normal`
- Findings: `2`

## [HIGH] gc.long_pause_p95
- Message: GC p95 pause exceeds threshold.
- Threshold: gc_pause_p95_ms > 250.0

## [WARNING] platform.cpu_throttle_gc_coincide
- Message: Долгие паузы GC совпадают с CPU throttle: нехватка CPU, не настройка G1.
