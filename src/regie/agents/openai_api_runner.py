"""Dependency-free Responses API loop with bounded local repository tools."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _inside(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("path escapes repository root") from exc
    return candidate


def _clip(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "\n[... truncated; request a narrower range]"


def _run_tool(name: str, arguments: dict[str, Any], cfg: dict,
              root: Path) -> tuple[str, int]:
    limit = int(cfg["token_policy"]["tool_result_chars"])
    if name == "list_files":
        pattern = str(arguments["glob"])
        proc = subprocess.run(["rg", "--files", "-g", pattern], cwd=root,
                              capture_output=True, text=True, timeout=30, check=False)
        raw = proc.stdout or proc.stderr
    elif name == "read_file":
        path = _inside(root, str(arguments["path"]))
        start = max(1, int(arguments["start_line"]))
        end = max(start, min(start + 500, int(arguments["end_line"])))
        lines = path.read_text(errors="replace").splitlines()
        raw = "\n".join(f"{number}: {lines[number - 1]}"
                        for number in range(start, min(end, len(lines)) + 1))
    elif name == "search":
        path = _inside(root, str(arguments["path"]))
        maximum = max(1, min(200, int(arguments["max_results"])))
        proc = subprocess.run(
            ["rg", "-n", "--max-count", str(maximum), "--",
             str(arguments["query"]), str(path)], cwd=root,
            capture_output=True, text=True, timeout=30, check=False)
        raw = proc.stdout or proc.stderr
    elif name == "apply_patch":
        if cfg["token_policy"]["sandbox"] != "workspace-write":
            raise ValueError("patch tool is disabled in read-only mode")
        patch = str(arguments["patch"])
        with tempfile.NamedTemporaryFile("w", suffix=".diff", delete=False) as handle:
            handle.write(patch)
            patch_path = handle.name
        try:
            checked = subprocess.run(["git", "apply", "--check", patch_path], cwd=root,
                                     capture_output=True, text=True, timeout=30, check=False)
            if checked.returncode != 0:
                raw = "patch rejected: " + checked.stderr
            else:
                applied = subprocess.run(["git", "apply", "--whitespace=nowarn", patch_path],
                                         cwd=root, capture_output=True, text=True, timeout=30,
                                         check=False)
                raw = "patch applied" if applied.returncode == 0 else applied.stderr
        finally:
            Path(patch_path).unlink(missing_ok=True)
    elif name == "run_check":
        command_name = str(arguments["name"])
        commands = cfg.get("allowed_commands") or {}
        if command_name not in commands:
            raise ValueError(f"unknown allowed command: {command_name}")
        proc = subprocess.run(commands[command_name], cwd=root, shell=True,
                              capture_output=True, text=True, timeout=600, check=False)
        raw = f"exit={proc.returncode}\n{proc.stdout}{proc.stderr}"
    else:
        raise ValueError(f"unknown tool: {name}")
    return _clip(raw, limit), len(raw.encode())


def _tools(cfg: dict) -> list[dict]:
    enabled = set(cfg["token_policy"]["tools"])
    tools: list[dict] = []
    definitions = {
        "list": ("list_files", "List repository files matching a glob.", {
            "type": "object", "additionalProperties": False,
            "properties": {"glob": {"type": "string"}}, "required": ["glob"]}),
        "read": ("read_file", "Read a bounded line range from a repository file.", {
            "type": "object", "additionalProperties": False,
            "properties": {"path": {"type": "string"},
                           "start_line": {"type": "integer"},
                           "end_line": {"type": "integer"}},
            "required": ["path", "start_line", "end_line"]}),
        "search": ("search", "Search text under one repository path.", {
            "type": "object", "additionalProperties": False,
            "properties": {"query": {"type": "string"}, "path": {"type": "string"},
                           "max_results": {"type": "integer"}},
            "required": ["query", "path", "max_results"]}),
        "patch": ("apply_patch", "Apply a unified git patch after validation.", {
            "type": "object", "additionalProperties": False,
            "properties": {"patch": {"type": "string"}}, "required": ["patch"]}),
        "shell": ("run_check", "Run one exact harness-configured check by name.", {
            "type": "object", "additionalProperties": False,
            "properties": {"name": {"type": "string",
                                      "enum": sorted((cfg.get("allowed_commands") or {}).keys())}},
            "required": ["name"]}),
    }
    if cfg["token_policy"]["sandbox"] == "read-only":
        enabled.discard("patch")
    if not cfg.get("allowed_commands"):
        enabled.discard("shell")
    for key in ("list", "read", "search", "patch", "shell"):
        if key not in enabled:
            continue
        name, description, parameters = definitions[key]
        tools.append({"type": "function", "name": name, "description": description,
                      "parameters": parameters, "strict": True})
    return tools


def _post(payload: dict, timeout: float) -> dict:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    request = Request(
        base + "/responses", data=json.dumps(payload).encode(), method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def _text(response: dict) -> str:
    parts: list[str] = []
    for item in response.get("output") or []:
        if item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if content.get("type") in ("output_text", "text"):
                parts.append(str(content.get("text", "")))
    return "\n".join(parts)


def _usage_metrics(responses: list[dict], tool_bytes: int) -> tuple[dict, dict]:
    raw = {"input_tokens": 0, "cached_input_tokens": 0, "cache_write_input_tokens": 0,
           "output_tokens": 0, "reasoning_output_tokens": 0}
    for response in responses:
        usage = response.get("usage") or {}
        details = usage.get("input_tokens_details") or {}
        output_details = usage.get("output_tokens_details") or {}
        raw["input_tokens"] += int(usage.get("input_tokens") or 0)
        raw["cached_input_tokens"] += int(details.get("cached_tokens") or 0)
        raw["cache_write_input_tokens"] += int(details.get("cache_write_tokens") or 0)
        raw["output_tokens"] += int(usage.get("output_tokens") or 0)
        raw["reasoning_output_tokens"] += int(output_details.get("reasoning_tokens") or 0)
    metrics = {
        "new_input_tokens": max(0, raw["input_tokens"] - raw["cached_input_tokens"]
                                - raw["cache_write_input_tokens"]),
        "cached_input_tokens": raw["cached_input_tokens"],
        "cache_write_input_tokens": raw["cache_write_input_tokens"],
        "output_tokens": raw["output_tokens"],
        "reasoning_output_tokens": raw["reasoning_output_tokens"],
        "tool_output_bytes": tool_bytes,
        "cost_usd": 0,
    }
    return raw, metrics


def run(cfg: dict) -> dict:
    root = Path(cfg["cwd"]).resolve()
    tools = _tools(cfg)
    payload: dict[str, Any] = {
        "model": cfg["binding"]["model"],
        "instructions": cfg.get("instructions") or "",
        "input": cfg["prompt"],
        "reasoning": {"effort": cfg["token_policy"]["effort"],
                      "context": "current_turn"},
        "text": {"verbosity": "low"},
        "tools": tools,
    }
    if cfg.get("output_schema"):
        payload["text"]["format"] = {
            "type": "json_schema", "name": "regie_result", "strict": True,
            "schema": cfg["output_schema"]}
    responses: list[dict] = []
    tool_bytes = 0
    timeout = max(30.0, float(cfg["budgets"]["wall_minutes"]) * 60)
    for turn in range(1, int(cfg["budgets"]["turns"]) + 1):
        response = _post(payload, timeout)
        responses.append(response)
        print(json.dumps({"type": "response", "turn": turn,
                          "response_id": response.get("id")}), flush=True)
        calls = [item for item in response.get("output") or []
                 if item.get("type") == "function_call"]
        if calls:
            outputs = []
            for call in calls:
                try:
                    arguments = json.loads(call.get("arguments") or "{}")
                    output, size = _run_tool(call["name"], arguments, cfg, root)
                    tool_bytes += size
                except Exception as exc:  # noqa: BLE001 - tool errors return to model
                    output = f"tool error: {exc}"
                outputs.append({"type": "function_call_output",
                                "call_id": call["call_id"], "output": output})
            payload = {
                "model": cfg["binding"]["model"],
                "instructions": cfg.get("instructions") or "",
                "previous_response_id": response["id"],
                "input": outputs,
                "reasoning": {"effort": cfg["token_policy"]["effort"],
                              "context": "current_turn"},
                "text": payload["text"], "tools": tools,
            }
            continue
        text = _text(response)
        raw, metrics = _usage_metrics(responses, tool_bytes)
        if response.get("error") or (response.get("status") == "incomplete" and not text):
            detail = response.get("error") or response.get("incomplete_details") or {}
            return {"regie_result": True, "outcome": "error",
                    "text": f"Responses API incomplete: {detail}",
                    "usage": raw, "metrics": metrics, "turns": turn}
        if not text:
            return {"regie_result": True, "outcome": "error",
                    "text": "Responses API returned no message text",
                    "usage": raw, "metrics": metrics, "turns": turn}
        structured = None
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                structured = parsed
        except json.JSONDecodeError:
            pass
        for line in text.splitlines():
            if line.strip().lower().startswith("blocked:"):
                return {"regie_result": True, "outcome": "blocked", "text": text,
                        "blocked_question": line.split(":", 1)[1].strip(),
                        "usage": raw, "metrics": metrics, "turns": turn}
        return {"regie_result": True, "outcome": "done", "text": text,
                "structured": structured, "usage": raw, "metrics": metrics,
                "turns": turn}
    raw, metrics = _usage_metrics(responses, tool_bytes)
    return {"regie_result": True, "outcome": "error",
            "text": "Responses API tool-turn budget exhausted",
            "usage": raw, "metrics": metrics, "turns": len(responses)}


def main() -> None:
    config_path = Path(sys.argv[1])
    try:
        cfg = json.loads(config_path.read_text())
    finally:
        config_path.unlink(missing_ok=True)
    try:
        result = run(cfg)
    except HTTPError as exc:
        outcome = "quota" if exc.code == 429 else "error"
        result = {"regie_result": True, "outcome": outcome,
                  "text": f"OpenAI API HTTP {exc.code}: {exc.read().decode(errors='replace')[-1500:]}"}
    except (URLError, RuntimeError, ValueError) as exc:
        result = {"regie_result": True, "outcome": "error", "text": str(exc)}
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
