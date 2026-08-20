"""Typed analysis session: CLI Namespace and UI request both become this."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field, fields
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = _ROOT / "thresholds.yaml"
DEFAULT_TUNING = _ROOT / "knowledge" / "prod_tuning.yaml"
DEFAULT_PLAYBOOK = _ROOT / "knowledge" / "symptom_playbook.yaml"


@dataclass
class AnalysisSession:
    """One pipeline run. Attribute names match CLI flags (dest names)."""

    output_dir: Path
    report: Path | None = None
    config: Path = DEFAULT_CONFIG
    compare_run: Path | None = None
    run_a_id: str = "run_a"
    run_b_id: str = "run_b"
    compare_settings: Path | None = None
    compare_prod: Path | None = None
    stable_prod_reports: list[Path] | None = None
    stable_prod_label: list[str] = field(default_factory=list)
    min_stability: float = 1.0
    tuning: Path = DEFAULT_TUNING
    symptom: str | None = None
    symptom_reports: list[Path] | None = None
    symptom_label: list[str] = field(default_factory=list)
    query_hex: str | None = None
    query_id: str | None = None
    query_text: str | None = None
    playbook: Path = DEFAULT_PLAYBOOK
    nt_reports: list[Path] | None = None
    nt_label: list[str] = field(default_factory=list)
    prod_reports: list[Path] | None = None
    prod_label: list[str] = field(default_factory=list)
    dml_etalon_reports: list[Path] | None = None
    dml_etalon_label: list[str] = field(default_factory=list)
    symptoms: str | None = None
    settings_a_id: str = "NT"
    settings_b_id: str = "PROD"
    confluence_title: str | None = None
    min_change_pct: float = 5.0
    top_n: int = 15
    exit_code: bool = False
    exit_code_quality: bool = False


def session_from_namespace(ns: argparse.Namespace) -> AnalysisSession:
    """CLI adapter: copy argparse dests onto AnalysisSession."""
    values: dict[str, object] = {}
    for item in fields(AnalysisSession):
        if hasattr(ns, item.name):
            values[item.name] = getattr(ns, item.name)
    return AnalysisSession(**values)  # type: ignore[arg-type]


def coerce_analysis_session(args: argparse.Namespace | AnalysisSession) -> AnalysisSession:
    if isinstance(args, AnalysisSession):
        return args
    return session_from_namespace(args)
