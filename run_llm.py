#!/usr/bin/env python3
"""Run a headless LLM task over an existing analysis output directory.

Examples:
  python run_llm.py --list-providers
  python run_llm.py --output-dir analysis_out --task summary --print-bundle
  python run_llm.py --output-dir analysis_out --task tuning --provider qwen_local
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pgprofile_llm import (
    DEFAULT_LLM_CONFIG,
    LLMError,
    LLMRequest,
    build_provider,
    describe_providers,
    load_llm_config,
    probe_llm_connection,
)
from pgprofile_llm_policy import describe_policy
from pgprofile_llm_tasks import (
    DEFAULT_MAX_CHARS,
    TASK_PRESETS,
    build_prompt_bundle,
    list_tasks,
    write_llm_artifacts,
)
from pgprofile_llm_validate import record_llm_quality


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Headless LLM analysis of pg_profile artifacts")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("analysis_out"),
        help="Analysis output directory with brief/influence artifacts (default: analysis_out)",
    )
    parser.add_argument(
        "--task",
        choices=sorted(TASK_PRESETS),
        default="summary",
        help="Task preset (default: summary)",
    )
    parser.add_argument(
        "--provider",
        help="Provider name from the config; defaults to default_provider / PGPROFILE_LLM_PROVIDER",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"LLM provider config (default: {DEFAULT_LLM_CONFIG.name})",
    )
    parser.add_argument(
        "--extra-instructions",
        default="",
        help="Short additional requirement appended to the task (manual override)",
    )
    parser.add_argument("--temperature", type=float, help="Override provider temperature")
    parser.add_argument("--max-tokens", type=int, help="Override provider max_tokens")
    parser.add_argument(
        "--max-chars",
        type=int,
        default=DEFAULT_MAX_CHARS,
        help=f"Prompt size limit in characters (default: {DEFAULT_MAX_CHARS})",
    )
    parser.add_argument(
        "--print-bundle",
        action="store_true",
        help="Print the assembled prompt and exit without calling the provider",
    )
    parser.add_argument(
        "--list-providers", action="store_true", help="Show configured providers and exit"
    )
    parser.add_argument("--list-tasks", action="store_true", help="Show task presets and exit")
    parser.add_argument(
        "--list-policy",
        action="store_true",
        help="Show the active LLM send policy and exit",
    )
    parser.add_argument(
        "--check-connection",
        action="store_true",
        help="Send a minimal request to verify endpoint, auth and model name; writes nothing",
    )
    return parser


def _print_providers(config_path: Path | None) -> int:
    rows = describe_providers(load_llm_config(config_path))
    print(f"{'provider':<16} {'type':<14} {'model':<28} default token")
    for row in rows:
        token = "-"
        if row["token_env"]:
            token = f"{row['token_env']}={'set' if row['token_present'] else 'MISSING'}"
        print(
            f"{row['provider']:<16} {row['type']:<14} {row['model'][:28]:<28} "
            f"{'yes' if row['is_default'] else 'no':<7} {token}"
        )
    return 0


def _check_connection(args: argparse.Namespace) -> int:
    """Smallest possible round trip: separates setup problems from prompt problems."""
    status = probe_llm_connection(
        load_llm_config(args.config),
        provider_name=args.provider,
    )
    info = {
        "provider": status.provider,
        "type": status.provider_type,
        "model": status.model,
        "url": status.url,
    }
    print(f"Provider: {json.dumps(info, ensure_ascii=False)}")
    if status.failed or status.skipped:
        print(f"CONNECTION_FAILED: {status.reason}", file=sys.stderr)
        if status.trace_id:
            print(f"trace_id: {status.trace_id}", file=sys.stderr)
        return 1
    print(f"Model reported: {status.model}")
    print(f"Latency: {status.latency_ms} ms")
    print(f"Answer: {status.answer_preview}")
    print(f"trace_id: {status.trace_id}")
    print("CONNECTION_OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        if args.list_providers:
            return _print_providers(args.config)
        if args.list_tasks:
            for task in list_tasks():
                print(f"{task['task']:<14} {task['title']}")
                print(f"{'':<14} {task['goal']}")
            return 0
        if args.list_policy:
            print(json.dumps(describe_policy(), ensure_ascii=False, indent=2))
            return 0
        if args.check_connection:
            return _check_connection(args)

        bundle = build_prompt_bundle(
            args.output_dir,
            task=args.task,
            overrides={
                "extra_instructions": args.extra_instructions,
                "temperature": args.temperature,
                "max_tokens": args.max_tokens,
            },
            max_chars=args.max_chars,
        )
    except LLMError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"Task: {bundle.task} ({bundle.metadata.get('task_title')})")
    print(f"Sources: {', '.join(bundle.sources)}")
    print(f"Prompt size: {bundle.char_count()} chars")
    if bundle.metadata.get("trimmed_sections"):
        print(f"Trimmed sections: {', '.join(bundle.metadata['trimmed_sections'])}")
    print(f"Trace id: {bundle.trace_id}")
    policy_meta = (bundle.metadata or {}).get("policy") or {}
    print(f"Policy: {policy_meta.get('name') or 'none'}"
          f" (sanitization={'on' if policy_meta.get('sanitization') else 'off'})")

    if args.print_bundle:
        print("\n--- SYSTEM ---")
        print(bundle.system)
        print("\n--- PROMPT ---")
        print(bundle.prompt)
        return 0

    try:
        provider = build_provider(load_llm_config(args.config), provider_name=args.provider)
    except LLMError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    info = provider.describe()
    print(f"Provider: {json.dumps(info, ensure_ascii=False)}")

    try:
        response = provider.generate(bundle.to_request())
    except LLMError as exc:
        written = write_llm_artifacts(
            args.output_dir, bundle, error=exc, provider_info=info
        )
        print(f"error: {exc}", file=sys.stderr)
        for path in written:
            print(f"  {path.name}")
        return 1

    written = write_llm_artifacts(
        args.output_dir, bundle, response=response, provider_info=info
    )
    print(
        f"Answer: {len(response.text)} chars in {response.latency_ms} ms"
        f" (attempts={response.attempts}, finish_reason={response.finish_reason or 'n/a'})"
    )
    for path in written:
        print(f"  {path.name}")
    quality = record_llm_quality(
        args.output_dir,
        response.text,
        task=bundle.task,
        dry_run=response.finish_reason == "dry_run" or info.get("provider") == "dry_run",
    )
    print(
        f"Quality: {quality.get('score')}/100 ({quality.get('verdict')}), "
        f"publishable={'yes' if quality.get('publishable') else 'no'}"
    )
    if quality.get("quality_file"):
        print(f"  {quality['quality_file']}")
    print("  oracle_report.json")
    if not quality.get("publishable"):
        print("PUBLISH_BLOCKED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
