# jvmcheck

Python utility for JVM tuning recommendations in Kubernetes/OpenShift workloads.

## What it does

- Parses container CPU/Memory requests/limits from YAML-like resource files.
- Parses mixed JVM options format from custom config files (for example `resources/jvmconf.txt`).
- Detects common runtime issues (long GC pauses, old gen pressure, high memory limit usage).
- Produces Java Tool Options recommendations with JDK-aware baselines.
- Supports optional runtime context (JDK, Spring Boot, hints) but works when context is missing.
- Builds quota-aware memory increase plans that account for pod-level memory budget and neighboring containers.
- Uses profile-driven thresholds from `thresholds_jvm.yaml` (`conservative`, `normal`, `aggressive`, `oltp-api`, `batch`, `streaming`, `latency-critical`).
- Supports multi-run stability analysis from previous JSON snapshots (`--multi-run-snapshot`).

## Run

```bash
pip install -e .

# Variant 1: explicit files
jvmcheck \
  --resources-file resources/resources_example_2.txt \
  --jvm-config-file resources/jvmconf.txt \
  --container-name application \
  --old-gen-used-mib 4200 \
  --old-gen-capacity-mib 5000 \
  --gc-pause-p95-ms 320 \
  --gc-pause-p99-ms 550 \
  --container-memory-working-set-mib 9600 \
  --jdk-version 17 \
  --spring-boot-version 3.3.1

# Variant 2: system directory auto-discovery
jvmcheck \
  --systems-root resources \
  --system-name EFSFinmonitoringWeb \
  --container-name application \
  --old-gen-used-mib 4200 \
  --old-gen-capacity-mib 5000 \
  --gc-pause-p95-ms 320 \
  --gc-pause-p99-ms 550 \
  --container-memory-working-set-mib 9600 \
  --threshold-profile oltp-api \
  --multi-run-snapshot history/run_20260720.json \
  --multi-run-snapshot history/run_20260721.json

# Variant 3: Confluence wiki output
jvmcheck \
  --systems-root resources \
  --system-name EFSFinmonitoringWeb \
  --container-name application \
  --old-gen-used-mib 4200 \
  --old-gen-capacity-mib 5000 \
  --gc-pause-p95-ms 320 \
  --gc-pause-p99-ms 550 \
  --container-memory-working-set-mib 9600 \
  --output-format confluence
```

## Expected directory layout

```text
resources/
  EFSFinmonitoringWeb/
    resources.yaml
    jvm-config.yaml (or .txt)
  AnotherSystem/
    values-resources.yaml
    java-options.txt
```

When `--system-name` is used, the tool selects resource YAML and JVM config from that system folder automatically.

## Confluence output

Use `--output-format confluence` to print wiki markup (`h2.`, `h3.`, `||table||`, `{code}` blocks), suitable for direct paste into a Confluence page.  
The output includes:
- findings and recommended JVM flags;
- step-by-step validation runbook for engineers;
- risk and side-effect notes for each class of changes;
- escalation guidance when tuning does not improve metrics.
- stable/ephemeral findings block when multi-run snapshots are passed.

## Knowledge and consistency

- `knowledge/jvm_recommendations.yaml` — rule-id -> recommendations/actions/risks/references.
- `knowledge/jvm_flag_matrix.yaml` — JDK applicability and known flag conflicts.
- Validate knowledge consistency:

```bash
python scripts/check_knowledge_consistency.py
```

## AI follow-up bundle (for weak models)

If your organization uses a weak model (for example, DeepSeek V4 flash), generate constrained artifacts:

```bash
jvmcheck \
  --resources-file resources/resources_example.txt \
  --jvm-config-file resources/jvmconf.txt \
  --container-name application \
  --gc-pause-p99-ms 6000 \
  --heap-used-mib 1600 \
  --prepare-ai-analysis-bundle \
  --ai-analysis-root ai_analysis \
  --ai-model-label "DeepSeek V4 flash (262k)"
```

This creates a timestamped directory with:
- `input_snapshot.json` (facts only),
- `approved_sources.md` (Java community allowlist),
- `gates.md` (anti-hallucination gates),
- `response_schema.json` (strict output schema),
- `ai_task_prompt.md` (ready prompt),
- `README.md` (workflow).

## Validate AI response (PASS/FAIL)

After the model returns JSON, validate it against gates and allowlist:

```bash
jvmcheck \
  --validate-ai-response-file ai_response.json \
  --ai-bundle-dir ai_analysis/20260721T111230Z_application
```

Validation checks:
- required JSON structure,
- per-recommendation required fields,
- citations only from `approved_sources.md`,
- evidence references to known metric keys,
- gate checklist statuses are pass/ok,
- confidence discipline (`high` only with enough context).

