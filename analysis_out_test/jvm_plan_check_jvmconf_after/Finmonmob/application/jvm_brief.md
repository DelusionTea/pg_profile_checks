# JVM Analysis Brief

- System: `Finmonmob`
- Container: `application`
- Selected problems: ``
- Threshold profile: `normal`
- Findings: `2`

## [WARNING] memory.request_pressure
- Message: Working set significantly exceeds memory request.
- Threshold: working_set/request >= 1.15

## [INFO] oldgen.metric_missing
- Message: OldGen utilization is not provided. OldGen recommendations may be less precise.
- Threshold: provide old_gen_used_percent or old_gen_used_mib
