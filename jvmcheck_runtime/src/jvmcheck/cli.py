from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from jvmcheck.ai.bundle_builder import build_ai_analysis_bundle
from jvmcheck.ai.response_validator import validate_ai_response
from jvmcheck.analyzers.multi_run_analyzer import analyze_multi_run_stability, multi_run_to_dict
from jvmcheck.analyzers.jvm_health_analyzer import analyze_jvm_health
from jvmcheck.formatters.confluence_formatter import format_analysis_for_confluence
from jvmcheck.input_resolver import resolve_system_input_files
from jvmcheck.models import RuntimeContext, RuntimeMetrics
from jvmcheck.parsers.custom_config_parser import parse_jvm_options_file
from jvmcheck.parsers.k8s_yaml_parser import parse_k8s_or_stand_yaml
from jvmcheck.recommenders.java_tool_options_recommender import enrich_with_recommendations
from jvmcheck.thresholds import load_thresholds
from jvmcheck.validation import InputValidationError, validate_runtime_metrics


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Analyze JVM/Kubernetes resource settings.")
    parser.add_argument("--resources-file", help="Path to Kubernetes/OpenShift resource YAML.")
    parser.add_argument("--jvm-config-file", help="Path to custom JVM config like jvmconf.txt.")
    parser.add_argument(
        "--systems-root",
        default="resources",
        help="Root directory with per-system folders. Example: resources/<SystemName>/...",
    )
    parser.add_argument("--system-name", help="System directory name inside --systems-root.")
    parser.add_argument("--container-name", help="Target container for analysis.")

    parser.add_argument("--heap-used-mib", type=int)
    parser.add_argument("--heap-committed-mib", type=int)
    parser.add_argument("--old-gen-used-mib", type=int)
    parser.add_argument("--old-gen-capacity-mib", type=int)
    parser.add_argument("--gc-pause-p95-ms", type=float)
    parser.add_argument("--gc-pause-p99-ms", type=float)
    parser.add_argument("--gc-time-ratio-percent", type=float)
    parser.add_argument("--container-memory-working-set-mib", type=int)
    parser.add_argument("--tuning-failed-after-previous-attempt", action="store_true")

    parser.add_argument("--jdk-version", type=int)
    parser.add_argument("--spring-boot-version")
    parser.add_argument("--framework-hint", action="append", default=[], help="Format key=value")
    parser.add_argument(
        "--thresholds-file",
        type=Path,
        help="Path to thresholds_jvm.yaml (optional override).",
    )
    parser.add_argument(
        "--threshold-profile",
        default="normal",
        help="Threshold profile: conservative|normal|aggressive|oltp-api|batch|streaming|latency-critical",
    )
    parser.add_argument(
        "--multi-run-snapshot",
        action="append",
        default=[],
        help="Path to previous jvmcheck JSON output for stability analysis (repeatable).",
    )
    parser.add_argument(
        "--multi-run-min-stability",
        type=float,
        default=0.6,
        help="Min ratio for stable findings across runs (default: 0.6).",
    )
    parser.add_argument(
        "--baseline-snapshot",
        help="Path to baseline jvmcheck JSON output for regression detection.",
    )
    parser.add_argument(
        "--prepare-ai-analysis-bundle",
        action="store_true",
        help="Create directory with gated AI-analysis artifacts.",
    )
    parser.add_argument(
        "--ai-analysis-root",
        default="ai_analysis",
        help="Output root for AI-analysis bundles.",
    )
    parser.add_argument(
        "--ai-model-label",
        default="DeepSeek V4 flash (262k)",
        help="Model label documented inside AI bundle.",
    )
    parser.add_argument(
        "--validate-ai-response-file",
        help="Path to AI response JSON for validation.",
    )
    parser.add_argument(
        "--ai-bundle-dir",
        help="Path to previously generated AI bundle directory.",
    )
    parser.add_argument(
        "--output-format",
        choices=("json", "confluence"),
        default="json",
        help="Output format. Use 'confluence' for Confluence wiki markup.",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.validate_ai_response_file:
            if not args.ai_bundle_dir:
                raise ValueError("Pass --ai-bundle-dir together with --validate-ai-response-file.")
            validation = validate_ai_response(
                bundle_dir=Path(args.ai_bundle_dir),
                response_file=Path(args.validate_ai_response_file),
            )
            print(json.dumps(validation, ensure_ascii=False, indent=2))
            return

        if not args.resources_file and not args.system_name:
            raise ValueError("Pass --resources-file or --system-name.")

        resources_file, jvm_config_file = resolve_system_input_files(
            systems_root=Path(args.systems_root),
            system_name=args.system_name or "",
            resources_file=args.resources_file,
            jvm_config_file=args.jvm_config_file,
        )

        budget = parse_k8s_or_stand_yaml(
            resources_file.read_text(encoding="utf-8"),
            source_path=resources_file,
        )
        if not budget.containers:
            raise InputValidationError(
                "No containers with resources found in input file.",
                file_path=resources_file,
                hint="Expected `resources.requests/limits` in at least one container.",
            )

        custom_options = {}
        if jvm_config_file:
            custom_options = parse_jvm_options_file(jvm_config_file)

        target_container = _choose_target_container(budget, args.container_name)
        if target_container.name in custom_options:
            target_container.java_tool_options = custom_options[target_container.name]

        metrics = RuntimeMetrics(
            heap_used_mib=args.heap_used_mib,
            heap_committed_mib=args.heap_committed_mib,
            old_gen_used_mib=args.old_gen_used_mib,
            old_gen_capacity_mib=args.old_gen_capacity_mib,
            gc_pause_p95_ms=args.gc_pause_p95_ms,
            gc_pause_p99_ms=args.gc_pause_p99_ms,
            gc_time_ratio_percent=args.gc_time_ratio_percent,
            container_memory_working_set_mib=args.container_memory_working_set_mib,
        )
        metric_errors = validate_runtime_metrics(metrics)
        if metric_errors:
            raise metric_errors[0]

        context = RuntimeContext(
            jdk_version=args.jdk_version,
            spring_boot_version=args.spring_boot_version,
            framework_hints=_parse_framework_hints(args.framework_hint),
        )

        thresholds = load_thresholds(
            profile=args.threshold_profile,
            path=args.thresholds_file,
        )
        analysis = analyze_jvm_health(
            container=target_container,
            metrics=metrics,
            tuning_failed_after_previous_attempt=args.tuning_failed_after_previous_attempt,
            threshold_set=thresholds,
        )
        analysis = enrich_with_recommendations(
            container=target_container,
            budget=budget,
            analysis=analysis,
            runtime_context=context,
        )
        trend = None
        snapshot_paths = [Path(path) for path in (args.multi_run_snapshot or [])]
        baseline_path = Path(args.baseline_snapshot) if args.baseline_snapshot else None
        if snapshot_paths:
            trend = analyze_multi_run_stability(
                analysis,
                snapshot_paths,
                min_stability_ratio=args.multi_run_min_stability,
                baseline_path=baseline_path,
            )

        ai_bundle_dir: Path | None = None
        if args.prepare_ai_analysis_bundle:
            ai_bundle_dir = build_ai_analysis_bundle(
                analysis=analysis,
                container=target_container,
                budget=budget,
                runtime_metrics=metrics,
                runtime_context=context,
                system_name=args.system_name,
                resources_file=resources_file,
                jvm_config_file=jvm_config_file,
                output_root=Path(args.ai_analysis_root),
                model_label=args.ai_model_label,
            )

        if args.output_format == "confluence":
            text = format_analysis_for_confluence(
                analysis=analysis,
                container=target_container,
                runtime_metrics=metrics,
                runtime_context=context,
                system_name=args.system_name,
                trend=trend,
            )
            if ai_bundle_dir:
                text = (
                    text.rstrip()
                    + "\n\nh3. AI Follow-up Bundle\n"
                    + f"* Bundle directory: {{code}}{ai_bundle_dir}{{code}}\n"
                    + "* Use files in this directory for constrained AI post-analysis.\n"
                )
            print(text)
            return

        out = asdict(analysis)
        out["_threshold_profile"] = args.threshold_profile
        if trend:
            out["trend_analysis"] = multi_run_to_dict(trend)
        if ai_bundle_dir:
            out["_ai_analysis_bundle_dir"] = str(ai_bundle_dir)
            out["_ai_model_label"] = args.ai_model_label
        print(json.dumps(out, ensure_ascii=False, indent=2))
    except InputValidationError as exc:
        raise SystemExit(f"Validation error: {exc}") from exc


def _choose_target_container(budget, requested_name: str | None):
    if requested_name:
        for container in budget.containers:
            if container.name == requested_name:
                return container
        raise ValueError(f"Container '{requested_name}' not found in resources input.")

    for preferred in ("application", "app"):
        for container in budget.containers:
            if container.name == preferred:
                return container
    return max(budget.containers, key=lambda c: c.limits.memory_mib or 0)


def _parse_framework_hints(values: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in values:
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        parsed[key.strip()] = value.strip()
    return parsed


if __name__ == "__main__":
    main()

