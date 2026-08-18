#!/usr/bin/env python3
"""End-to-end scenarios on the three real pg_profile reports in resources/.

Covers the CLI pair path, the NT series path, and the UI one-click Qwen runner
(dry_run plus in-process stand-ins for qwen_local / qwen_gateway). Outputs land in
analysis_out_test/e2e/ so they can be inspected after a run.

Usage: python scripts/check_e2e.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REPORT_JUL31 = ROOT / "resources" / "pgprofile_srv=10_3_81_94_from=2026_07_31_09_00_to=2026_08_01_00.html"
REPORT_AUG11 = ROOT / "resources" / "pgprofile_srv=10_3_81_94_from=2026_08_11_16_30_to=2026_08_12_09.html"
REPORT_AUG12 = ROOT / "resources" / "pgprofile_srv=10_3_81_94_from=2026_08_12_16_00_to=2026_08_13_08.html"
OUT_ROOT = ROOT / "analysis_out_test" / "e2e"
PAIR_DIR = OUT_ROOT / "pair"
SERIES_DIR = OUT_ROOT / "series"
QWEN_DRY_DIR = OUT_ROOT / "qwen_dry"
QWEN_LOCAL_DIR = OUT_ROOT / "qwen_local"
QWEN_GATEWAY_DIR = OUT_ROOT / "qwen_gateway"

PAIR_FILES = (
    "health_check.json",
    "run_comparison.json",
    "settings_diff.json",
    "influence_table.json",
    "influence_table.csv",
    "influence_summary.md",
    "influence_summary.wiki",
    "findings.json",
    "advisor.json",
    "brief.md",
    "oracle_report.json",
    "oracle_report.md",
    "quality_report.json",
    "quality_report.md",
)
SERIES_FILES = (
    "nt_runs.json",
    "influence_table_series.json",
    "influence_table_series.csv",
    "influence_summary_series.md",
    "influence_summary_series.wiki",
    "nt_runs_brief.md",
    "nt_runs_confluence.wiki",
    "findings.json",
    "advisor.json",
    "oracle_report.json",
    "oracle_report.md",
    "quality_report.json",
    "quality_report.md",
)
LLM_FILES = (
    "llm_request_summary.json",
    "llm_response_summary.json",
    "llm_summary.md",
    "llm_quality_summary.json",
    "llm_job.json",
)
SUMMARY_SECTIONS = ("Сопоставимость нагрузки", "Достоверность", "Правила трактовки")

GROUNDED_ANSWER = {
    "verdict": "need-validation",
    "summary": (
        "shared_buffers вырос вместе с cache.postgres.blks_read; "
        "связь probable, изолированного изменения нет."
    ),
    "claims": [
        {
            "statement": "Рост shared_buffers совпал с ростом cache.postgres.blks_read.",
            "subject": "shared_buffers",
            "evidence_type": "probable",
        }
    ],
    "recommendations": [
        {
            "parameter": "shared_buffers",
            "action": "повторить на изолированном прогоне, не меняя остальные GUC",
        }
    ],
    "risks": ["в паре сменились сразу несколько параметров"],
    "missing_data": [],
}


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise AssertionError(f"{path.name}: expected a JSON object")
    return payload


def _run_analyze(argv: list[str]) -> int:
    import analyze_pgprofile

    saved = sys.stdout
    sys.stdout = io.StringIO()
    try:
        return analyze_pgprofile.main(argv)
    finally:
        sys.stdout = saved


def _require_files(output_dir: Path, names: tuple[str, ...], failures: list[str], prefix: str) -> None:
    for name in names:
        if not (output_dir / name).is_file():
            failures.append(f"{prefix}: missing {name}")


def _check_contract(path: Path, failures: list[str], prefix: str) -> None:
    from pgprofile_contracts import validate_contract_payload

    try:
        validate_contract_payload(_load(path))
    except ValueError as exc:
        failures.append(f"{prefix}: {path.name} failed contract validation: {exc}")


def _check_oracle_quality(
    output_dir: Path,
    failures: list[str],
    prefix: str,
    *,
    expected_layers: tuple[str, ...],
    allow_fail: bool = False,
) -> None:
    oracle = _load(output_dir / "oracle_report.json")
    if oracle.get("type") != "oracle_report":
        failures.append(f"{prefix}: oracle_report.json has unexpected type")
    verdict = oracle.get("verdict")
    if verdict not in {"pass", "warning", "fail"}:
        failures.append(f"{prefix}: oracle verdict is {verdict!r}")
    if verdict == "fail" and not allow_fail:
        reasons = ", ".join(str(item) for item in (oracle.get("reasons") or [])[:6])
        failures.append(f"{prefix}: oracle verdict is fail ({reasons})")
    if verdict == "fail" and allow_fail and not (oracle.get("reasons") or []):
        failures.append(f"{prefix}: oracle fail has no reasons")
    layer_names = {item.get("name") for item in (oracle.get("layers") or []) if isinstance(item, dict)}
    for name in expected_layers:
        if name not in layer_names:
            failures.append(f"{prefix}: oracle is missing layer {name!r}")

    quality = _load(output_dir / "quality_report.json")
    if quality.get("type") != "quality_report":
        failures.append(f"{prefix}: quality_report.json has unexpected type")
    if not quality.get("confidence_trail"):
        failures.append(f"{prefix}: quality_report.json has an empty confidence_trail")


def _check_influence_table(
    path: Path,
    failures: list[str],
    prefix: str,
    *,
    expected_type: str,
    must_mention: str,
) -> list[dict[str, Any]]:
    payload = _load(path)
    if payload.get("type") != expected_type:
        failures.append(f"{prefix}: {path.name} type is {payload.get('type')!r}")
    rows = [row for row in (payload.get("rows") or []) if isinstance(row, dict)]
    if not rows:
        failures.append(f"{prefix}: {path.name} has no rows")
        return []
    params = {str(row.get("parameter") or "") for row in rows}
    if must_mention not in params:
        failures.append(f"{prefix}: {path.name} does not mention {must_mention}")
    return rows


def _check_summary_text(path: Path, failures: list[str], prefix: str, rows: list[dict[str, Any]]) -> None:
    text = path.read_text(encoding="utf-8")
    for heading in SUMMARY_SECTIONS:
        if heading not in text:
            failures.append(f"{prefix}: {path.name} is missing {heading!r}")
    for row in rows:
        param = str(row.get("parameter") or "")
        if param and str(row.get("affected_metric") or "").strip() and param not in text:
            failures.append(f"{prefix}: {path.name} does not mention linked parameter {param}")


def _check_compare_ui_source(failures: list[str]) -> None:
    app_js = (ROOT / "ui" / "web" / "js" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "ui" / "web" / "css" / "pgprofile.css").read_text(encoding="utf-8")
    markers = (
        ("Сопоставимость нагрузки низкая", "workload warning copy"),
        ('compareSelect("impact"', "impact filter"),
        ('compareSelect("evidence_type"', "evidence_type filter"),
        ('compareSelect("sort"', "sort control"),
        ("probable — гипотеза", "probable/proven hint"),
        ("Изменённые параметры", "changed GUC list"),
        ("filterAndSortInfluenceRows", "influence filter/sort helper"),
        ("data-compare-filter=", "filter select wiring"),
        ("summary.compare", "compare view-model field"),
    )
    for needle, label in markers:
        if needle not in app_js:
            failures.append(f"compare UI: app.js is missing {label}")
    if ".compare-filters" not in css:
        failures.append("compare UI: css is missing .compare-filters")


def _check_compare_summary(output_dir: Path, failures: list[str], prefix: str) -> None:
    from ui.analysis_runner import _build_summary

    summary = _build_summary(output_dir)
    influence = summary.get("influence") or {}
    rows = [row for row in (influence.get("rows") or []) if isinstance(row, dict)]
    if not rows:
        failures.append(f"{prefix}: UI summary has no influence rows")
        return
    params = [str(row.get("parameter") or "") for row in rows]
    if prefix == "pair" and "shared_buffers" not in params:
        failures.append(f"{prefix}: UI summary GUC list is missing shared_buffers")
    trail = (summary.get("quality") or {}).get("confidence_trail") or []
    if not trail:
        failures.append(f"{prefix}: UI summary is missing quality.confidence_trail")
    workload = influence.get("workload_match") or {}
    if "level" not in workload and "workload_match_score" not in workload:
        failures.append(f"{prefix}: UI summary is missing workload_match")
    compare = summary.get("compare") or {}
    if compare.get("mode") not in {"pair", "series"}:
        failures.append(f"{prefix}: UI summary.compare.mode is {compare.get('mode')!r}")
    elif prefix == "series" and compare.get("mode") != "series":
        failures.append(f"{prefix}: UI summary.compare.mode should be series")
    elif prefix == "pair" and compare.get("mode") != "pair":
        failures.append(f"{prefix}: UI summary.compare.mode should be pair")
    if not isinstance(compare.get("changed_params"), list) or not compare.get("changed_params"):
        failures.append(f"{prefix}: UI summary.compare.changed_params is empty")
    if not isinstance(compare.get("workload_weak"), bool):
        failures.append(f"{prefix}: UI summary.compare.workload_weak is missing")
    if "confidence_hints" not in compare:
        failures.append(f"{prefix}: UI summary.compare.confidence_hints is missing")


def run_pair(failures: list[str]) -> None:
    if PAIR_DIR.exists():
        shutil.rmtree(PAIR_DIR)
    code = _run_analyze(
        [
            "--report",
            str(REPORT_AUG11),
            "--compare-run",
            str(REPORT_AUG12),
            "--compare-settings",
            str(REPORT_AUG12),
            "--output-dir",
            str(PAIR_DIR),
        ]
    )
    if code not in (0, 1):
        failures.append(f"pair: analyze_pgprofile exited {code}")
        return

    _require_files(PAIR_DIR, PAIR_FILES, failures, "pair")
    if not (PAIR_DIR / "influence_table.json").is_file():
        return
    for name in ("run_comparison.json", "settings_diff.json", "influence_table.json"):
        _check_contract(PAIR_DIR / name, failures, "pair")
    rows = _check_influence_table(
        PAIR_DIR / "influence_table.json",
        failures,
        "pair",
        expected_type="influence_table",
        must_mention="shared_buffers",
    )
    if rows:
        for row in rows:
            count = row.get("evidence_count")
            if count not in (0, 1):
                failures.append(
                    f"pair: evidence_count must be 0 or 1, got {count!r} on {row.get('parameter')}"
                )
        _check_summary_text(PAIR_DIR / "influence_summary.md", failures, "pair", rows)
        _check_summary_text(PAIR_DIR / "influence_summary.wiki", failures, "pair", rows)
    if (PAIR_DIR / "oracle_report.json").is_file() and (PAIR_DIR / "quality_report.json").is_file():
        _check_oracle_quality(PAIR_DIR, failures, "pair", expected_layers=("rule_based",))
        _check_compare_summary(PAIR_DIR, failures, "pair")


def run_series(failures: list[str]) -> None:
    if SERIES_DIR.exists():
        shutil.rmtree(SERIES_DIR)
    code = _run_analyze(
        [
            "--nt-reports",
            str(REPORT_JUL31),
            str(REPORT_AUG11),
            str(REPORT_AUG12),
            "--nt-label",
            "nt1",
            "--nt-label",
            "nt2",
            "--nt-label",
            "nt3",
            "--symptoms",
            "high_cpu",
            "--output-dir",
            str(SERIES_DIR),
        ]
    )
    if code not in (0, 1):
        failures.append(f"series: analyze_pgprofile exited {code}")
        return

    _require_files(SERIES_DIR, SERIES_FILES, failures, "series")
    series_path = SERIES_DIR / "influence_table_series.json"
    if not series_path.is_file():
        return
    _check_contract(series_path, failures, "series")
    rows = _check_influence_table(
        series_path,
        failures,
        "series",
        expected_type="influence_table_series",
        must_mention="shared_buffers",
    )
    payload = _load(series_path)
    reports = ((payload.get("run_identity") or {}).get("reports") or []) if isinstance(
        payload.get("run_identity"), dict
    ) else []
    if len(reports) != 3:
        failures.append(f"series: expected 3 reports in run_identity, got {len(reports)}")
    for row in rows or []:
        if row.get("parameter") != "checkpoint_completion_target":
            continue
        metric = row.get("affected_metric")
        delta = row.get("delta_pct")
        if (
            metric == "checkpoint_write_time"
            and isinstance(delta, (int, float))
            and float(delta) > 0
            and row.get("impact") == "improved"
        ):
            failures.append(
                "series: checkpoint_completion_target impact=improved on "
                "+checkpoint_write_time (dominant-metric polarity)"
            )
    if rows:
        _check_summary_text(SERIES_DIR / "influence_summary_series.md", failures, "series", rows)
        _check_summary_text(SERIES_DIR / "influence_summary_series.wiki", failures, "series", rows)
        wiki = (SERIES_DIR / "nt_runs_confluence.wiki").read_text(encoding="utf-8")
        if "shared_buffers" not in wiki:
            failures.append("series: nt_runs_confluence.wiki does not mention shared_buffers")
    from ui.analysis_runner import _build_summary

    series_summary = _build_summary(SERIES_DIR)
    influence = series_summary.get("influence") or {}
    if influence.get("mode") != "series" and influence.get("type") != "influence_table_series":
        failures.append(
            f"series: UI summary mode is {influence.get('mode')!r} type={influence.get('type')!r}"
        )
    settings = influence.get("settings_table") or {}
    if not (settings.get("run_labels") or []):
        failures.append("series: UI summary is missing settings_table.run_labels")
    series_table = json.loads((SERIES_DIR / "influence_table_series.json").read_text(encoding="utf-8"))
    series_settings = series_table.get("settings_table") or {}
    if "equal_rows" not in series_settings:
        failures.append("series: settings_table is missing equal_rows")
    if not (series_settings.get("equal_rows") or []):
        failures.append("series: settings_table.equal_rows is empty")
    wiki_text = (SERIES_DIR / "nt_runs_confluence.wiki").read_text(encoding="utf-8")
    if "Одинаковые настройки" not in wiki_text:
        failures.append("series: wiki is missing the unchanged-GUC expand")
    if (SERIES_DIR / "oracle_report.json").is_file() and (SERIES_DIR / "quality_report.json").is_file():
        _check_oracle_quality(
            SERIES_DIR,
            failures,
            "series",
            expected_layers=("rule_based", "statistical"),
        )
        _check_compare_summary(SERIES_DIR, failures, "series")


@contextlib.contextmanager
def _stub_qwen(*, require_bearer: str | None = None) -> Iterator[tuple[str, dict[str, Any]]]:
    seen: dict[str, Any] = {"posts": 0, "authorization": None, "path": ""}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            seen["posts"] += 1
            seen["authorization"] = self.headers.get("Authorization")
            seen["path"] = self.path
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            if require_bearer is not None and seen["authorization"] != f"Bearer {require_bearer}":
                body = json.dumps({"error": "unauthorized"}).encode("utf-8")
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            content = json.dumps(GROUNDED_ANSWER, ensure_ascii=False)
            payload = {
                "id": "e2e-completion",
                "model": "qwen-e2e",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 8, "completion_tokens": 8},
            }
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}/v1", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _copy_pair(dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(PAIR_DIR, dest)


def _check_llm_job(
    output_dir: Path,
    job: dict[str, Any],
    failures: list[str],
    prefix: str,
    *,
    provider: str,
    publishable: bool,
    expect_text: str,
) -> None:
    from ui.llm_runner import read_answer

    if job.get("status") != "success":
        failures.append(f"{prefix}: job status is {job.get('status')!r}, error={job.get('error')}")
        return
    if job.get("provider") != provider:
        failures.append(f"{prefix}: provider is {job.get('provider')!r}, expected {provider!r}")
    if bool(job.get("publishable")) != publishable:
        failures.append(
            f"{prefix}: publishable is {job.get('publishable')!r}, expected {publishable}"
        )
    _require_files(output_dir, LLM_FILES, failures, prefix)
    answer = read_answer(output_dir)
    text = (answer or {}).get("text") or ""
    if expect_text not in text:
        failures.append(f"{prefix}: answer text does not contain {expect_text!r}")
    quality = _load(output_dir / "quality_report.json")
    llm = quality.get("llm") if isinstance(quality.get("llm"), dict) else {}
    if not llm.get("present"):
        failures.append(f"{prefix}: quality_report.llm.present is not true")
    if llm.get("publishable") is not publishable:
        failures.append(f"{prefix}: quality_report.llm.publishable is {llm.get('publishable')!r}")
    layer_names = {item.get("name") for item in (quality.get("layers") or []) if isinstance(item, dict)}
    if "llm" not in layer_names:
        failures.append(f"{prefix}: quality report is missing llm layer")


def run_qwen(failures: list[str]) -> None:
    if not (PAIR_DIR / "brief.md").is_file():
        failures.append("qwen: pair artifacts are missing, cannot start one-click jobs")
        return

    from pgprofile_llm import QwenGatewayProvider, QwenLocalProvider
    from ui.llm_runner import start_llm_job

    _copy_pair(QWEN_DRY_DIR)
    dry_job = start_llm_job(QWEN_DRY_DIR, task="summary", provider_name="dry_run", wait=True)
    _check_llm_job(
        QWEN_DRY_DIR,
        dry_job,
        failures,
        "qwen_dry",
        provider="dry_run",
        publishable=False,
        expect_text="dry-run",
    )

    _copy_pair(QWEN_LOCAL_DIR)
    with _stub_qwen() as (base_url, seen_local):
        local = QwenLocalProvider(
            name="qwen_local",
            base_url=base_url,
            model="qwen-e2e",
            timeout_sec=5,
            max_retries=0,
        )
        local_job = start_llm_job(
            QWEN_LOCAL_DIR,
            task="summary",
            provider_name="qwen_local",
            provider=local,
            wait=True,
        )
    if seen_local["posts"] != 1:
        failures.append(f"qwen_local: stub received {seen_local['posts']} POST(s), expected 1")
    if seen_local["authorization"]:
        failures.append("qwen_local: local provider must not send Authorization")
    if not str(seen_local["path"]).endswith("/chat/completions"):
        failures.append(f"qwen_local: unexpected path {seen_local['path']!r}")
    _check_llm_job(
        QWEN_LOCAL_DIR,
        local_job,
        failures,
        "qwen_local",
        provider="qwen_local",
        publishable=True,
        expect_text="shared_buffers",
    )

    _copy_pair(QWEN_GATEWAY_DIR)
    previous_token = os.environ.get("PGPROFILE_LLM_TOKEN")
    os.environ["PGPROFILE_LLM_TOKEN"] = "e2e-token"
    try:
        with _stub_qwen(require_bearer="e2e-token") as (base_url, seen_gw):
            gateway = QwenGatewayProvider(
                name="qwen_gateway",
                base_url=base_url,
                model="qwen-e2e",
                timeout_sec=5,
                max_retries=0,
                auth={"token_env": "PGPROFILE_LLM_TOKEN"},
            )
            gateway_job = start_llm_job(
                QWEN_GATEWAY_DIR,
                task="summary",
                provider_name="qwen_gateway",
                provider=gateway,
                wait=True,
            )
    finally:
        if previous_token is None:
            os.environ.pop("PGPROFILE_LLM_TOKEN", None)
        else:
            os.environ["PGPROFILE_LLM_TOKEN"] = previous_token
    if seen_gw["posts"] != 1:
        failures.append(f"qwen_gateway: stub received {seen_gw['posts']} POST(s), expected 1")
    if seen_gw["authorization"] != "Bearer e2e-token":
        failures.append(
            f"qwen_gateway: expected Bearer e2e-token, got {seen_gw['authorization']!r}"
        )
    _check_llm_job(
        QWEN_GATEWAY_DIR,
        gateway_job,
        failures,
        "qwen_gateway",
        provider="qwen_gateway",
        publishable=True,
        expect_text="shared_buffers",
    )


def main() -> int:
    missing = [path for path in (REPORT_JUL31, REPORT_AUG11, REPORT_AUG12) if not path.is_file()]
    if missing:
        print("E2E_CHECK_FAILED")
        for path in missing:
            print(f"- missing report: {path}")
        return 1

    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    _check_compare_ui_source(failures)
    run_pair(failures)
    run_series(failures)
    run_qwen(failures)

    if failures:
        print("E2E_CHECK_FAILED")
        for item in failures:
            print(f"- {item}")
        return 1

    print(f"Checked e2e artifacts in {OUT_ROOT}")
    print(f"  pair={PAIR_DIR.name} series={SERIES_DIR.name}")
    print("  qwen=dry_run (blocked) + qwen_local + qwen_gateway (publishable)")
    print("E2E_CHECK_PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
