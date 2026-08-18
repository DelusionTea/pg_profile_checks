# JVM Analysis Brief

- System: `DEMO_CounterAgent`
- Container: `application`
- Selected problems: ``
- Threshold profile: `normal`
- Findings: `4`

## [WARNING] memory.request_pressure
- Message: Working set significantly exceeds memory request.
- Threshold: working_set/request >= 1.15

## [INFO] heap.metric_missing
- Message: Heap utilization is not provided. Recommendations may be less precise.
- Threshold: provide heap_used_mib or heap_used_percent

## [INFO] oldgen.metric_missing
- Message: OldGen utilization is not provided. OldGen recommendations may be less precise.
- Threshold: provide old_gen_used_percent or old_gen_used_mib

## [CRITICAL] platform.non_heap_accumulation
- Message: RSS контейнера растёт, при этом после GC heap снизился. GC работает. Увеличьте memory limit; G1 не копируйте.
