#!/usr/bin/env python3
"""Orchestrate pg_profile analysis and produce deterministic JSON + advisor brief."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from pgprofile_advisor import (
    advise_findings,
    advisor_report_to_dict,
    build_brief,
    build_llm_prompt,
)
from pgprofile_confluence import (
    build_confluence_llm_prompt,
    build_confluence_stub,
    write_nt_prod_confluence_outputs,
    write_stable_prod_confluence_outputs,
    write_symptom_confluence_outputs,
)
from pgprofile_compare import compare_runs, load_run
from pgprofile_findings import (
    health_check_to_dict,
    run_comparison_to_dict,
    settings_diff_to_dict,
)
from pgprofile_health import load_report_data, load_thresholds, run_checks
from pgprofile_parser import PgProfileParseError, load_settings, parse_report_meta
from pgprofile_nt_prod import nt_prod_validation_to_dict, validate_nt_prod
from pgprofile_stable_prod import analyze_stable_prod, stable_prod_to_dict
from pgprofile_nt_runs import (
    analyze_nt_runs,
    build_nt_runs_brief,
    build_nt_runs_confluence_wiki,
    nt_runs_to_dict,
    parse_symptom_list,
)
from pgprofile_symptoms import QueryTarget, investigate_symptom, symptom_investigation_to_dict

from compare_settings import diff_settings

DEFAULT_CONFIG = Path(__file__).resolve().parent / "thresholds.yaml"
DEFAULT_TUNING = Path(__file__).resolve().parent / "knowledge" / "prod_tuning.yaml"
DEFAULT_PLAYBOOK = Path(__file__).resolve().parent / "knowledge" / "symptom_playbook.yaml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run pg_profile analysis pipeline and produce advisor output for LLM."
    )
    parser.add_argument("--report", type=Path, help="Single report for health check")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--compare-run",
        type=Path,
        help="Second report for run comparison (requires --report as first run)",
    )
    parser.add_argument("--run-a-id", type=str, default="run_a")
    parser.add_argument("--run-b-id", type=str, default="run_b")
    parser.add_argument("--compare-settings", type=Path, help="Second report for settings diff")
    parser.add_argument(
        "--compare-prod",
        type=Path,
        help="PROD report for NT vs PROD validation (--report = NT); writes nt_prod_confluence_*",
    )
    parser.add_argument(
        "--stable-prod-reports",
        nargs="+",
        type=Path,
        metavar="HTML",
        help="Two or more PROD reports for stable-problem analysis; writes stable_prod_*",
    )
    parser.add_argument(
        "--stable-prod-label",
        action="append",
        default=[],
        metavar="NAME",
        help="Label for --stable-prod-reports (repeat per file, same order)",
    )
    parser.add_argument(
        "--min-stability",
        type=float,
        default=1.0,
        help="Min fraction of PROD reports for stable finding (default: 1.0 = all)",
    )
    parser.add_argument(
        "--tuning",
        type=Path,
        default=DEFAULT_TUNING,
        help=f"PROD tuning rules YAML (default: {DEFAULT_TUNING.name})",
    )
    parser.add_argument(
        "--symptom",
        type=str,
        help="Symptom to investigate: high_cpu | high_memory | high_wal | slow_query",
    )
    parser.add_argument(
        "--symptom-reports",
        nargs="+",
        type=Path,
        metavar="HTML",
        help="pg_profile HTML for symptom investigation (1+ files); writes symptom_*",
    )
    parser.add_argument(
        "--symptom-label",
        action="append",
        default=[],
        metavar="NAME",
        help="Label for --symptom-reports (repeat per file, same order)",
    )
    parser.add_argument(
        "--query-hex",
        type=str,
        help="Target query hexqueryid (required for --symptom slow_query)",
    )
    parser.add_argument(
        "--query-id",
        type=str,
        help="Target query queryid (for --symptom slow_query)",
    )
    parser.add_argument(
        "--query-text",
        type=str,
        help="SQL text substring (for --symptom slow_query)",
    )
    parser.add_argument(
        "--playbook",
        type=Path,
        default=DEFAULT_PLAYBOOK,
        help=f"Symptom playbook YAML (default: {DEFAULT_PLAYBOOK.name})",
    )
    parser.add_argument(
        "--nt-reports",
        nargs="+",
        type=Path,
        metavar="HTML",
        help="2+ NT reports in order; with --symptoms runs multi-run analysis + GUC impact",
    )
    parser.add_argument(
        "--nt-label",
        action="append",
        default=[],
        metavar="NAME",
        help="Label for --nt-reports (repeat per file, same order)",
    )
    parser.add_argument(
        "--symptoms",
        type=str,
        help="Comma/space-separated symptoms for --nt-reports: high_cpu,high_wal,...",
    )
    parser.add_argument("--settings-a-id", type=str, default="NT")
    parser.add_argument("--settings-b-id", type=str, default="PROD")
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory for findings.json, advisor.json, brief.md, summary_prompt.txt, confluence_*",
    )
    parser.add_argument(
        "--confluence-title",
        type=str,
        help="Custom Confluence page title (default: auto from report metadata)",
    )
    parser.add_argument("--min-change-pct", type=float, default=5.0)
    parser.add_argument("--top-n", type=int, default=15)
    parser.add_argument(
        "--exit-code",
        action="store_true",
        help="Exit 1 if any analysis has findings/issues",
    )
    return parser


def _save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    if (
        not args.report
        and not args.compare_settings
        and not args.stable_prod_reports
        and not (args.symptom and args.symptom_reports)
        and not (args.nt_reports and args.symptoms)
    ):
        print(
            "error: provide --report, --compare-settings, --stable-prod-reports, "
            "--symptom with --symptom-reports, and/or --nt-reports with --symptoms",
            file=sys.stderr,
        )
        return 2

    if args.nt_reports and not args.symptoms:
        print("error: --nt-reports requires --symptoms", file=sys.stderr)
        return 2
    if args.symptoms and not args.nt_reports:
        print("error: --symptoms requires --nt-reports", file=sys.stderr)
        return 2
    if args.nt_reports and len(args.nt_reports) < 2:
        print("error: --nt-reports requires at least two HTML files", file=sys.stderr)
        return 2
    if args.nt_label and len(args.nt_label) != len(args.nt_reports or []):
        print("error: --nt-label count must match --nt-reports", file=sys.stderr)
        return 2

    if args.symptom and not args.symptom_reports:
        print("error: --symptom requires --symptom-reports", file=sys.stderr)
        return 2
    if args.symptom_reports and not args.symptom:
        print("error: --symptom-reports requires --symptom", file=sys.stderr)
        return 2
    if args.symptom_label and len(args.symptom_label) != len(args.symptom_reports or []):
        print("error: --symptom-label count must match --symptom-reports", file=sys.stderr)
        return 2

    if args.stable_prod_reports and len(args.stable_prod_reports) < 2:
        print("error: --stable-prod-reports requires at least two HTML files", file=sys.stderr)
        return 2
    if args.min_stability <= 0 or args.min_stability > 1:
        print("error: --min-stability must be in (0, 1]", file=sys.stderr)
        return 2
    if args.stable_prod_label and len(args.stable_prod_label) != len(args.stable_prod_reports or []):
        print(
            "error: --stable-prod-label count must match --stable-prod-reports",
            file=sys.stderr,
        )
        return 2

    analyses: list[dict[str, Any]] = []
    has_issues = False
    stable_prod_analysis = None
    symptom_investigation = None
    nt_runs_analysis = None

    try:
        if args.report:
            cfg = load_thresholds(args.config)
            ctx = load_report_data(args.report)
            warnings = run_checks(ctx, cfg)
            health = health_check_to_dict(ctx, warnings)
            analyses.append(health)
            _save_json(args.output_dir / "health_check.json", health)
            if warnings:
                has_issues = True

            if args.compare_run:
                run_a = load_run(args.report, args.run_a_id)
                run_b = load_run(args.compare_run, args.run_b_id)
                result = compare_runs(
                    run_a,
                    run_b,
                    min_change_pct=args.min_change_pct,
                    top_n=args.top_n,
                )
                run_cmp = run_comparison_to_dict(
                    run_a, run_b, result, min_change_pct=args.min_change_pct
                )
                analyses.append(run_cmp)
                _save_json(args.output_dir / "run_comparison.json", run_cmp)
                if run_cmp.get("findings"):
                    has_issues = True

        if args.report and args.compare_settings:
            path_a = args.report
            path_b = args.compare_settings
            nt_settings = load_settings(path_a, defined_only=True)
            prod_settings = load_settings(path_b, defined_only=True)
            diffs = diff_settings(nt_settings, prod_settings)
            settings = settings_diff_to_dict(
                label_a=args.settings_a_id,
                label_b=args.settings_b_id,
                path_a=path_a,
                path_b=path_b,
                meta_a=parse_report_meta(path_a),
                meta_b=parse_report_meta(path_b),
                diffs=diffs,
            )
            analyses.append(settings)
            _save_json(args.output_dir / "settings_diff.json", settings)
            if settings.get("findings"):
                critical = settings.get("summary", {}).get("critical_count")
                if critical is None:
                    has_issues = True
                elif critical > 0:
                    has_issues = True

        if args.report and args.compare_prod:
            nt_prod = validate_nt_prod(
                args.report,
                args.compare_prod,
                min_change_pct=args.min_change_pct,
                top_n=args.top_n,
                nt_label=args.settings_a_id,
                prod_label=args.settings_b_id,
            )
            _save_json(
                args.output_dir / "nt_prod_validation.json",
                nt_prod_validation_to_dict(nt_prod),
            )
            write_nt_prod_confluence_outputs(
                nt_prod,
                args.output_dir,
                page_title=args.confluence_title,
            )
            if not nt_prod.settings.valid or nt_prod.warning_count > 0:
                has_issues = True

        if args.stable_prod_reports:
            labels = args.stable_prod_label if args.stable_prod_label else None
            for path in args.stable_prod_reports:
                if not path.exists():
                    raise FileNotFoundError(f"stable PROD report not found: {path}")
            stable_prod_analysis = analyze_stable_prod(
                args.stable_prod_reports,
                labels=labels,
                thresholds_path=args.config,
                min_stability_ratio=args.min_stability,
                tuning_path=args.tuning,
            )
            stable_dict = stable_prod_to_dict(stable_prod_analysis)
            analyses.append(stable_dict)
            _save_json(args.output_dir / "stable_prod.json", stable_dict)
            write_stable_prod_confluence_outputs(
                stable_prod_analysis,
                args.output_dir,
                page_title=args.confluence_title,
            )
            critical_severities = {"critical", "high"}
            if any(
                r.problem_severity in critical_severities
                for r in stable_prod_analysis.recommendations
            ):
                has_issues = True

        if args.nt_reports and args.symptoms:
            for path in args.nt_reports:
                if not path.exists():
                    raise FileNotFoundError(f"NT report not found: {path}")
            try:
                parse_symptom_list(args.symptoms)
            except ValueError as exc:
                raise ValueError(str(exc)) from exc
            query_target = QueryTarget(
                hexqueryid=args.query_hex,
                queryid=args.query_id,
                query_text=args.query_text,
            )
            labels = args.nt_label if args.nt_label else None
            nt_runs_analysis = analyze_nt_runs(
                args.nt_reports,
                labels=labels,
                symptoms=args.symptoms,
                playbook_path=args.playbook,
                health_thresholds_path=args.config,
                min_change_pct=args.min_change_pct,
                top_n=args.top_n,
                query_target=query_target,
            )
            nt_dict = nt_runs_to_dict(nt_runs_analysis)
            analyses.append(nt_dict)
            _save_json(args.output_dir / "nt_runs.json", nt_dict)
            (args.output_dir / "nt_runs_brief.md").write_text(
                build_nt_runs_brief(nt_runs_analysis), encoding="utf-8"
            )
            (args.output_dir / "nt_runs_confluence.wiki").write_text(
                build_nt_runs_confluence_wiki(
                    nt_runs_analysis, page_title=args.confluence_title
                ),
                encoding="utf-8",
            )
            if any(
                inv.causes
                for inv in nt_runs_analysis.symptom_investigations
                if any(c.status.value in ("confirmed", "suspected") for c in inv.causes)
            ):
                has_issues = True

        if args.symptom and args.symptom_reports:
            for path in args.symptom_reports:
                if not path.exists():
                    raise FileNotFoundError(f"symptom report not found: {path}")
            query_target = QueryTarget(
                hexqueryid=args.query_hex,
                queryid=args.query_id,
                query_text=args.query_text,
            )
            labels = args.symptom_label if args.symptom_label else None
            symptom_investigation = investigate_symptom(
                args.symptom,
                args.symptom_reports,
                labels=labels,
                query_target=query_target,
                playbook_path=args.playbook,
                health_thresholds_path=args.config,
            )
            symptom_dict = symptom_investigation_to_dict(symptom_investigation)
            analyses.append(symptom_dict)
            _save_json(args.output_dir / "symptom_investigation.json", symptom_dict)
            write_symptom_confluence_outputs(
                symptom_investigation,
                args.output_dir,
                page_title=args.confluence_title,
            )
            if symptom_dict.get("summary", {}).get("confirmed_count", 0) > 0:
                has_issues = True

    except (PgProfileParseError, FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    combined_findings: list[dict[str, Any]] = []
    for analysis in analyses:
        combined_findings.extend(analysis.get("findings", []))

    combined = {
        "type": "combined_analysis",
        "analyses": analyses,
        "findings": combined_findings,
        "summary": {
            "total_findings": len(combined_findings),
            "analysis_count": len(analyses),
        },
    }
    _save_json(args.output_dir / "findings.json", combined)

    advisor_analyses = [
        a
        for a in analyses
        if a.get("type")
        not in ("stable_prod_analysis", "symptom_investigation", "nt_runs_analysis")
    ]
    advisor_reports = [advise_findings(a) for a in advisor_analyses]
    combined_advisor = {
        "type": "combined_advisor",
        "reports": [advisor_report_to_dict(r) for r in advisor_reports],
        "summary": {
            "total_findings": len(combined_findings),
            "reports": len(advisor_reports),
        },
    }
    if stable_prod_analysis is not None:
        combined_advisor["stable_prod"] = stable_prod_to_dict(stable_prod_analysis)
    if nt_runs_analysis is not None:
        combined_advisor["nt_runs"] = nt_runs_to_dict(nt_runs_analysis)
    if symptom_investigation is not None:
        combined_advisor["symptom_investigation"] = symptom_investigation_to_dict(
            symptom_investigation
        )
    _save_json(args.output_dir / "advisor.json", combined_advisor)

    brief_parts = [build_brief(r) for r in advisor_reports if r.advised_findings]
    if stable_prod_analysis is not None:
        from pgprofile_stable_prod import build_stable_prod_brief

        brief_parts.append(build_stable_prod_brief(stable_prod_analysis))
    if nt_runs_analysis is not None:
        brief_parts.append(build_nt_runs_brief(nt_runs_analysis))
    if symptom_investigation is not None:
        from pgprofile_symptoms import build_symptom_brief

        brief_parts.append(build_symptom_brief(symptom_investigation))
    brief = "\n\n---\n\n".join(brief_parts) if brief_parts else ""
    if brief:
        (args.output_dir / "brief.md").write_text(brief, encoding="utf-8")

        prompt = build_llm_prompt(brief)
        (args.output_dir / "summary_prompt.txt").write_text(prompt, encoding="utf-8")

    if advisor_reports:
        confluence_stub = build_confluence_stub(
            advisor_reports, page_title=args.confluence_title
        )
        (args.output_dir / "confluence_stub.wiki").write_text(
            confluence_stub, encoding="utf-8"
        )

        confluence_prompt = build_confluence_llm_prompt(brief)
        (args.output_dir / "confluence_prompt.txt").write_text(
            confluence_prompt, encoding="utf-8"
        )

    print(f"Analysis written to {args.output_dir}/")
    if args.report:
        print(f"  health_check.json")
    if args.compare_run:
        print(f"  run_comparison.json")
    if args.compare_settings:
        print(f"  settings_diff.json")
    if stable_prod_analysis is not None:
        print(
            f"  stable_prod.json  ({len(stable_prod_analysis.recommendations)} recommendations)"
        )
        print(f"  stable_prod_confluence_stub.wiki  (стабильные PROD → Confluence)")
        print(f"  stable_prod_confluence_prompt.txt  (gigacli → план GUC)")
        print(f"  stable_prod_brief.md")
    if nt_runs_analysis is not None:
        print(f"  nt_runs.json  (symptoms: {', '.join(nt_runs_analysis.symptoms)})")
        print(f"  nt_runs_brief.md")
        print(f"  nt_runs_confluence.wiki  (симптомы + влияние GUC)")
    if symptom_investigation is not None:
        summary = symptom_investigation_to_dict(symptom_investigation)["summary"]
        print(
            f"  symptom_investigation.json  "
            f"({summary['confirmed_count']} confirmed, {summary['suspected_count']} suspected)"
        )
        print(f"  symptom_confluence_stub.wiki  (расследование симптома → Confluence)")
        print(f"  symptom_confluence_prompt.txt  (gigacli → диагностика)")
        print(f"  symptom_brief.md")
    print(f"  findings.json  ({len(combined_findings)} findings)")
    print(f"  advisor.json")
    if brief:
        print(f"  brief.md")
        print(f"  summary_prompt.txt  (ready for DeepSeek)")
    if advisor_reports:
        print(f"  confluence_stub.wiki  (metadata + findings table → Confluence)")
        print(f"  confluence_prompt.txt  (gigacli → Wiki Markup for Confluence)")
    if args.compare_prod:
        print(f"  nt_prod_validation.json")
        print(f"  nt_prod_confluence_stub.wiki  (НТ vs ПРОМ → Confluence)")
        print(f"  nt_prod_confluence_prompt.txt  (gigacli → краткая сводка НТ/ПРОМ)")
        print(f"  nt_prod_brief.md")

    if args.exit_code and has_issues:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
