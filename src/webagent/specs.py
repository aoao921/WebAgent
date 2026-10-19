from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolSpec:
    name: str
    display_name: str
    runtime: str
    category: str
    risk_level: str = "low"
    timeout_seconds: int = 45
    input_schema: dict[str, Any] = field(default_factory=dict)
