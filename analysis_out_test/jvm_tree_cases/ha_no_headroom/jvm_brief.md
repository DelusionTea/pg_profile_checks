# JVM Analysis Brief

- System: `DEMO_CounterAgent`
- Container: `application`
- Selected problems: ``
- Threshold profile: `normal`
- Findings: `2`

## [HIGH] gc.long_pause_p95
- Message: GC p95 pause exceeds threshold.
- Threshold: gc_pause_p95_ms > 250.0

## [CRITICAL] platform.ha_cpu_headroom
- Message: Нет запаса на отказ плеча: сумма CPU % of limits этой АС с двух плеч 90 > 80.
- Threshold: shoulder_cpu_pct_sum > 80
