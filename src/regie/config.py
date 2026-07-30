from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path

import yaml
from pydantic import BaseModel

from regie.models import Binding, Budgets


class ConfigError(Exception):
    pass


class Profile(BaseModel):
    name: str
    bindings: list[Binding]
    prompt_path: Path
    budgets: Budgets

    @property
    def primary(self) -> Binding:
        return self.bindings[0]

    def prompt_text(self) -> str:
        return self.prompt_path.read_text()

    def prompt_hash(self) -> str:
        return hashlib.sha256(self.prompt_path.read_bytes()).hexdigest()


class RegieConfig(BaseModel):
    commands: dict[str, str]
    test_globs: list[str]
    eval_trigger_globs: list[str] = []
    profiles: dict[str, Profile]
    base_branch: str = "main"


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
    if errors:
        raise ConfigError("; ".join(errors))

    return RegieConfig(
        commands=commands,
        test_globs=data["test_globs"],
        eval_trigger_globs=data.get("eval_trigger_globs", []),
        profiles=profiles,
        base_branch=data.get("base_branch", "main"),
    )
