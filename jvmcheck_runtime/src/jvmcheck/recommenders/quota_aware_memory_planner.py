from __future__ import annotations

from jvmcheck.models import ContainerResources, PodResourcesBudget, QuotaAwareMemoryPlan


def build_quota_aware_memory_plan(
    budget: PodResourcesBudget,
    target_container_name: str,
    requested_delta_mib: int,
) -> QuotaAwareMemoryPlan:
    if requested_delta_mib <= 0:
        return QuotaAwareMemoryPlan(
            status="fits",
            target_container=target_container_name,
            requested_delta_mib=0,
            notes=["No memory increase requested."],
        )

    target = _find_container(budget, target_container_name)
    if target is None:
        return QuotaAwareMemoryPlan(
            status="needs_platform_escalation",
            target_container=target_container_name,
            requested_delta_mib=requested_delta_mib,
            notes=["Target container not found in pod resources."],
        )

    pod_cap = budget.pod_memory_limit_mib or budget.summed_container_memory_limit_mib
    current_total = budget.summed_container_memory_limit_mib
    if current_total + requested_delta_mib <= pod_cap:
        return QuotaAwareMemoryPlan(
            status="fits",
            target_container=target_container_name,
            requested_delta_mib=requested_delta_mib,
            notes=["Requested memory increase fits within pod budget."],
        )

    needed = current_total + requested_delta_mib - pod_cap
    donor_suggestions = _find_memory_donors(budget, target_container_name, needed)
    donated_total = sum(donor_suggestions.values())
    if donated_total >= needed:
        return QuotaAwareMemoryPlan(
            status="needs_rebalance",
            target_container=target_container_name,
            requested_delta_mib=requested_delta_mib,
            donor_suggestions=donor_suggestions,
            notes=[
                "Pod budget exceeded; rebalance memory from neighboring containers.",
                f"Need to free at least {needed} MiB from other containers.",
            ],
        )

    return QuotaAwareMemoryPlan(
        status="needs_platform_escalation",
        target_container=target_container_name,
        requested_delta_mib=requested_delta_mib,
        donor_suggestions=donor_suggestions,
        notes=[
            "No safe in-pod memory rebalance found.",
            "Consider increasing pod/namespace quota or redesigning container resource profile.",
        ],
    )


def _find_container(budget: PodResourcesBudget, container_name: str) -> ContainerResources | None:
    for container in budget.containers:
        if container.name == container_name:
            return container
    return None


def _find_memory_donors(
    budget: PodResourcesBudget,
    target_container_name: str,
    needed_mib: int,
) -> dict[str, int]:
    donors: list[tuple[str, int]] = []
    for container in budget.containers:
        if container.name == target_container_name:
            continue
        limit = container.limits.memory_mib or 0
        request = container.requests.memory_mib or 0
        reserve = int(limit * 0.15)
        safe_give = max(limit - max(request, reserve), 0)
        if safe_give > 0:
            donors.append((container.name, safe_give))

    donors.sort(key=lambda pair: pair[1], reverse=True)

    allocated: dict[str, int] = {}
    remaining = needed_mib
    for name, can_give in donors:
        if remaining <= 0:
            break
        give = min(can_give, remaining)
        if give > 0:
            allocated[name] = give
            remaining -= give
    return allocated

