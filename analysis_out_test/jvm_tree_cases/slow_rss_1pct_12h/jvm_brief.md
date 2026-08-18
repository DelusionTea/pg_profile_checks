# JVM Analysis Brief

- System: `DEMO_CounterAgent`
- Container: `application`
- Selected problems: ``
- Threshold profile: `normal`
- Findings: `3`

## [INFO] heap.metric_missing
- Message: Heap utilization is not provided. Recommendations may be less precise.
- Threshold: provide heap_used_mib or heap_used_percent

## [INFO] oldgen.metric_missing
- Message: OldGen utilization is not provided. OldGen recommendations may be less precise.
- Threshold: provide old_gen_used_percent or old_gen_used_mib

## [INFO] platform.memory_growth_below_sla
- Message: Память растёт, но до SLA 80% больше 30 сут — не критично.
