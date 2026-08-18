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

## [WARNING] heap.rising_gc_floor
- Message: Очистка heap есть, но минимум после каждой следующей GC выше. Живые объекты копятся — это не плато.
