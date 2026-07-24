# JVM Analysis Brief

- System: `SberratingWeb`
- Container: `application`
- Selected problems: ``
- Threshold profile: `normal`
- Findings: `5`

## [WARNING] memory.request_pressure
- Message: Working set significantly exceeds memory request.
- Threshold: working_set/request >= 1.15

## [WARNING] jvm.flag_missing_container_support
- Message: JVM flag -XX:+UseContainerSupport is not explicitly configured.

## [WARNING] jvm.flag_missing_exit_on_oom
- Message: JVM flag -XX:+ExitOnOutOfMemoryError is not configured.

## [WARNING] jvm.flag_duplicate
- Message: Duplicate JVM flags detected.

## [INFO] oldgen.metric_missing
- Message: OldGen utilization is not provided. OldGen recommendations may be less precise.
- Threshold: provide old_gen_used_percent or old_gen_used_mib
