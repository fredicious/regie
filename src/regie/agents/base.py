from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from regie.models import Binding, Budgets


class AgentRequest(BaseModel):
    prompt: str
    cwd: Path
    binding: Binding
    budgets: Budgets
    output_schema: dict | None = None


class AgentResult(BaseModel):
    outcome: Literal["done", "blocked", "error", "quota"]
    text: str = ""
    structured: dict | None = None
    usage: dict = Field(default_factory=dict)
    turns: int = 0
    blocked_question: str | None = None


class AgentAdapter(Protocol):
    def build_command(self, req: AgentRequest) -> list[str]: ...
    def parse(self, stdout: str, exit_code: int) -> AgentResult: ...


_REGISTRY: dict[str, AgentAdapter] = {}


def register(cli: str, adapter: AgentAdapter) -> None:
    _REGISTRY[cli] = adapter


def get_adapter(cli: str) -> AgentAdapter:
    return _REGISTRY[cli]
