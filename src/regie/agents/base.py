from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from regie.models import Binding, Budgets, TokenPolicy, UsageMetrics


class AgentRequest(BaseModel):
    prompt: str
    instructions: str = ""
    cwd: Path
    binding: Binding
    budgets: Budgets
    token_policy: TokenPolicy = Field(default_factory=TokenPolicy)
    allowed_commands: dict[str, str] = Field(default_factory=dict)
    output_schema: dict | None = None


class AgentResult(BaseModel):
    outcome: Literal["done", "blocked", "error", "quota"]
    text: str = ""
    structured: dict | None = None
    usage: dict = Field(default_factory=dict)
    metrics: UsageMetrics = Field(default_factory=UsageMetrics)
    turns: int = 0
    blocked_question: str | None = None
    quota_kind: Literal["session", "weekly", "rate", "unknown"] | None = None
    quota_scope: Literal["provider", "model"] | None = None
    quota_reset_at: str | None = None
    quota_reason: str | None = None
    quota_synthetic: bool = False


class AgentAdapter(Protocol):
    def build_command(self, req: AgentRequest) -> list[str]: ...
    def parse(self, stdout: str, exit_code: int) -> AgentResult: ...


_REGISTRY: dict[str, AgentAdapter] = {}


def register(cli: str, adapter: AgentAdapter) -> None:
    _REGISTRY[cli] = adapter


def get_adapter(cli: str) -> AgentAdapter:
    return _REGISTRY[cli]
