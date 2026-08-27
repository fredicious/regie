from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from regie.models import Binding, Budgets, TokenPolicy, WorkflowTier


class ConfigError(Exception):
    pass


class Profile(BaseModel):
    name: str
    bindings: list[Binding]
    prompt_path: Path
    budgets: Budgets
    # Optional explicit "big gun" for complexity:hard tasks. Kept separate
    # from the list because the list orders preference-then-failover, not
    # strength — conflating them sent hard tasks to a WEAKER model (review
    # catch, 2026-07-31). Absent → hard tasks use the normal ladder.
    hard_binding: Binding | None = None
    token_policy: TokenPolicy = Field(default_factory=TokenPolicy)

    @property
    def primary(self) -> Binding:
        return self.bindings[0]

    def prompt_text(self) -> str:
        return self.prompt_path.read_text()

    def prompt_hash(self) -> str:
        return hashlib.sha256(self.prompt_path.read_bytes()).hexdigest()


class WorkflowConfig(BaseModel):
    default_tier: WorkflowTier = "auto"
    direct_execution: bool = True
    max_parallel_tasks: int = Field(default=1, ge=1, le=8)
    plan_reviews: bool = True
    design_reviews: bool = True
    final_review: bool = True
    knowledge: bool = True
    reflection: bool = True
    submit_pr: bool = True
    max_task_usd: float = Field(default=0.0, ge=0)
    max_run_usd: float = Field(default=0.0, ge=0)


class GatePlugin(BaseModel):
    name: str
    command: str
    stages: list[str] = Field(default_factory=lambda: ["finalize"])
    trigger_globs: list[str] = Field(default_factory=list)
    tiers: list[Literal["fast", "standard", "critical"]] = Field(
        default_factory=lambda: ["standard", "critical"])


class RegieConfig(BaseModel):
    commands: dict[str, str]
    test_globs: list[str]
    eval_trigger_globs: list[str] = []
    profiles: dict[str, Profile]
    base_branch: str = "main"
    workflow: WorkflowConfig = Field(default_factory=WorkflowConfig)
    gate_plugins: list[GatePlugin] = Field(default_factory=list)
    enabled_providers: set[str] = Field(default_factory=set)


def _bindings_of(name: str, raw: dict, errors: list[str]) -> list[Binding]:
    """`bindings:` wins over the legacy singular `binding:` key (a target repo
    can add the list without first deleting the old key). Emptiness is
    checked here, not via pydantic min_length, so the single ConfigError
    raised by load_config can name the offending profile."""
    if "bindings" in raw:
        if not raw["bindings"]:
            errors.append(f"profile '{name}' has an empty bindings list")
            return []
        return [Binding(**b) for b in raw["bindings"]]
    if "binding" in raw:
        return [Binding(**raw["binding"])]
    errors.append(f"profile '{name}' missing binding(s)")
    return []


def _load_profiles(profiles_dir: Path, errors: list[str]) -> dict[str, Profile]:
    profiles = {}
    for yml in sorted(profiles_dir.glob("*.yaml")):
        name = yml.stem
        prompt = profiles_dir / f"{name}.md"
        if not prompt.exists():
            errors.append(f"profile '{name}' missing prompt file {prompt.name}")
            continue
        raw = yaml.safe_load(yml.read_text())
        bindings = _bindings_of(name, raw, errors)
        if not bindings:
            continue
        profiles[name] = Profile(
            name=name,
            bindings=bindings,
            prompt_path=prompt,
            budgets=Budgets(**raw.get("budgets", {})),
            hard_binding=Binding(**raw["hard"]) if "hard" in raw else None,
            token_policy=TokenPolicy(**raw.get("token_policy", {})),
        )
    if not profiles:
        errors.append(f"no profiles found in {profiles_dir}")
    return profiles


def load_config(repo: Path, profiles_dir: Path) -> RegieConfig:
    errors: list[str] = []
    toml_path = repo / "regie.toml"
    if not toml_path.exists():
        raise ConfigError(f"missing {toml_path}")
    data = tomllib.loads(toml_path.read_text())

    commands = data.get("commands", {})
    for key in ("test", "lint"):
        if key not in commands:
            errors.append(f"missing required key commands.{key}")
    if "test_globs" not in data:
        errors.append("missing required key test_globs")

    profiles = _load_profiles(profiles_dir, errors)
    providers_section = data.get("providers", {})
    configured_providers = providers_section.get("enabled")
    if configured_providers is not None:
        if (not isinstance(configured_providers, list)
                or not all(isinstance(name, str) for name in configured_providers)):
            errors.append("providers.enabled must be a list of provider names")
        elif not configured_providers:
            errors.append("providers.enabled must contain at least one provider")
        else:
            enabled = set(configured_providers)
            for name, profile in list(profiles.items()):
                bindings = [binding for binding in profile.bindings
                            if binding.cli in enabled]
                if not bindings:
                    errors.append(
                        f"profile '{name}' has no bindings from enabled providers"
                    )
                    continue
                hard = profile.hard_binding
                if hard is not None and hard.cli not in enabled:
                    hard = None
                profiles[name] = profile.model_copy(update={
                    "bindings": bindings,
                    "hard_binding": hard,
                })
    if errors:
        raise ConfigError("; ".join(errors))

    enabled_providers = (
        set(configured_providers)
        if configured_providers is not None
        else {binding.cli for profile in profiles.values() for binding in profile.bindings}
    )

    plugins = []
    for name, raw in data.get("gates", {}).items():
        try:
            plugins.append(GatePlugin(name=name, **raw))
        except Exception as exc:
            raise ConfigError(f"invalid gate '{name}': {exc}") from exc

    return RegieConfig(
        commands=commands,
        test_globs=data["test_globs"],
        eval_trigger_globs=data.get("eval_trigger_globs", []),
        profiles=profiles,
        base_branch=data.get("base_branch", "main"),
        workflow=WorkflowConfig(**data.get("workflow", {})),
        gate_plugins=plugins,
        enabled_providers=enabled_providers,
    )
