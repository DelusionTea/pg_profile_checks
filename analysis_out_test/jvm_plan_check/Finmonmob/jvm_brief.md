# JVM Analysis Brief

- System: `Finmonmob`
- Container: `application`
- Selected problems: ``
- Threshold profile: `normal`
- Findings: `4`

## [WARNING] memory.request_pressure
- Message: Working set significantly exceeds memory request.
- Threshold: working_set/request >= 1.15

## [WARNING] jvm.flag_missing_container_support
- Message: JVM flag -XX:+UseContainerSupport is not explicitly configured.

## [WARNING] jvm.flag_missing_exit_on_oom
- Message: JVM flag -XX:+ExitOnOutOfMemoryError is not configured.

## [INFO] jvm.gc_not_declared
- Message: No explicit GC policy flag found.
