"""Write JSON analysis output to files or stdout."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def write_json_output(data: dict[str, Any], *, output: Path | None) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")
    else:
        sys.stdout.write(text + "\n")
