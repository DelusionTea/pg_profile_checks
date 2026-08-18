# JVM Analysis Brief

- System: `DEMO_CounterAgent`
- Container: `application`
- Selected problems: ``
- Threshold profile: `normal`
- Findings: `4`

## [CRITICAL] memory.limit_pressure
- Message: Container memory consumption is close to memory limit.
- Threshold: working_set/limit >= 0.80

## [WARNING] memory.request_pressure
- Message: Working set significantly exceeds memory request.
- Threshold: working_set/request >= 1.15

## [INFO] heap.metric_missing
- Message: Heap utilization is not provided. Recommendations may be less precise.
- Threshold: provide heap_used_mib or heap_used_percent

## [INFO] oldgen.metric_missing
- Message: OldGen utilization is not provided. OldGen recommendations may be less precise.
- Threshold: provide old_gen_used_percent or old_gen_used_mib
