from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class ResourceSpec:
    cpu_millicores: Optional[int] = None
    memory_mib: Optional[int] = None
    ephemeral_storage_mib: Optional[int] = None


@dataclass
class ContainerResources:
    name: str
    requests: ResourceSpec = field(default_factory=ResourceSpec)
    limits: ResourceSpec = field(default_factory=ResourceSpec)
    java_tool_options: List[str] = field(default_factory=list)


@dataclass
class PodResourcesBudget:
    containers: List[ContainerResources]
    pod_memory_limit_mib: Optional[int] = None
    pod_memory_request_mib: Optional[int] = None

    @property
    def summed_container_memory_limit_mib(self) -> int:
        return sum(c.limits.memory_mib or 0 for c in self.containers)

    @property
    def summed_container_memory_request_mib(self) -> int:
        return sum(c.requests.memory_mib or 0 for c in self.containers)


@dataclass
class RuntimeMetrics:
    heap_used_mib: Optional[int] = None
    heap_committed_mib: Optional[int] = None
    old_gen_used_mib: Optional[int] = None
    old_gen_capacity_mib: Optional[int] = None
    gc_pause_p95_ms: Optional[float] = None
    gc_pause_p99_ms: Optional[float] = None
    gc_time_ratio_percent: Optional[float] = None
    container_memory_working_set_mib: Optional[int] = None


@dataclass
class RuntimeContext:
    jdk_version: Optional[int] = None
    spring_boot_version: Optional[str] = None
    framework_hints: Dict[str, str] = field(default_factory=dict)


@dataclass
class Finding:
    code: str
    severity: str
    message: str
    evidence: Dict[str, str] = field(default_factory=dict)
    threshold: str = ""
    details: Dict[str, str] = field(default_factory=dict)


@dataclass
class Recommendation:
    title: str
    rationale: str
    suggested_java_tool_options: List[str] = field(default_factory=list)
    confidence: str = "medium"
    evidence_score: int = 50
    risk_score: int = 50
    expected_gain: str = ""
    verification_window: str = "30-60m after deploy"
    rollback_plan: List[str] = field(default_factory=list)
    rule_ids: List[str] = field(default_factory=list)
    blocking_conditions: List[str] = field(default_factory=list)
    requires_platform_escalation: bool = False
    notes: List[str] = field(default_factory=list)


@dataclass
class QuotaAwareMemoryPlan:
    status: str
    target_container: str
    requested_delta_mib: int
    donor_suggestions: Dict[str, int] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)


@dataclass
class AnalysisResult:
    findings: List[Finding] = field(default_factory=list)
    recommendations: List[Recommendation] = field(default_factory=list)
    memory_plan: Optional[QuotaAwareMemoryPlan] = None
    lifecycle_status: str = "tuning_attempted"


@dataclass
class FindingTrend:
    code: str
    severity: str
    occurrences: int
    total_runs: int
    stability_ratio: float
    sample_messages: List[str] = field(default_factory=list)


@dataclass
class MultiRunAnalysis:
    total_runs: int
    stable_findings: List[FindingTrend] = field(default_factory=list)
    ephemeral_findings: List[FindingTrend] = field(default_factory=list)
    regression_findings: List[FindingTrend] = field(default_factory=list)
    tuning_effectiveness: str = "unknown"

