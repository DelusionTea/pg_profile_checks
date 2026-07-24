# JVM Analysis Brief

- System: `Finmonweb`
- Container: `istioProxy`
- Selected problems: ``
- Threshold profile: `normal`
- Findings: `4`

## [WARNING] jvm.flag_missing_container_support
- Message: JVM flag -XX:+UseContainerSupport is not explicitly configured.

## [WARNING] jvm.flag_missing_exit_on_oom
- Message: JVM flag -XX:+ExitOnOutOfMemoryError is not configured.

## [INFO] jvm.gc_not_declared
- Message: No explicit GC policy flag found.

## [INFO] oldgen.metric_missing
- Message: OldGen utilization is not provided. OldGen recommendations may be less precise.
- Threshold: provide old_gen_used_percent or old_gen_used_mib
