from __future__ import annotations

import re
from typing import Optional


_MEMORY_UNITS_TO_MIB = {
    "Ki": 1 / 1024,
    "Mi": 1,
    "Gi": 1024,
    "Ti": 1024 * 1024,
}


def parse_cpu_to_millicores(value: object) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("m"):
        return int(float(text[:-1]))
    return int(float(text) * 1000)


def parse_memory_to_mib(value: object) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([A-Za-z]+)?", text)
    if not match:
        return None
    amount = float(match.group(1))
    unit = match.group(2) or "Mi"
    if unit in _MEMORY_UNITS_TO_MIB:
        return int(amount * _MEMORY_UNITS_TO_MIB[unit])
    # Kubernetes also accepts plain bytes without unit.
    if unit == "B":
        return int(amount / (1024 * 1024))
    return None

