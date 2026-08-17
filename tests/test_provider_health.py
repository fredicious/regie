from __future__ import annotations

from datetime import UTC, datetime, timedelta

from typer.testing import CliRunner

from regie.agents.base import AgentRequest, AgentResult
from regie.dispatch import run_agent
from regie.models import Binding, Budgets
from regie.provider_health import ProviderHealthStore, provider_key
from regie.quota import quota_metadata
from regie.rundir import RunDir

CLAUDE = Binding(cli="claude", model="opus", auth="subscription")
CODEX = Binding(cli="codex", model="gpt-5.6-sol", auth="subscription")


def test_quota_metadata_extracts_weekly_iso_reset():
    quota = quota_metadata(
        "Weekly limit reached; resets 2026-08-18T09:30:00+02:00")
    assert quota.kind == "weekly" and quota.scope == "provider"
    assert quota.reset_at == "2026-08-18T07:30:00+00:00"


def test_quota_metadata_extracts_retry_after_payload():
    now = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)
    quota = quota_metadata("rate limit", {"retry_after": 120}, now=now)
    assert quota.kind == "rate"
    assert quota.reset_at == "2026-08-14T08:02:00+00:00"


def test_provider_quota_is_shared_across_store_instances(regie_home):
    first = ProviderHealthStore(regie_home)
    second = ProviderHealthStore(regie_home)
    reset = "2026-08-14T13:00:00+00:00"
    first.record_quota(CLAUDE, AgentResult(
        outcome="quota", text="5-hour limit", quota_kind="session",
        quota_scope="provider", quota_reset_at=reset))

    decision = second.reserve(
        Binding(cli="claude", model="sonnet", auth="subscription"),
        now=datetime(2026, 8, 14, 9, 0, tzinfo=UTC))
    assert decision.allowed is False
    assert decision.key == provider_key(CLAUDE) and decision.reset_at == reset
    assert second.reserve(CODEX).allowed is True


def test_subscription_circuit_does_not_block_explicit_paid_auth(regie_home):
    store = ProviderHealthStore(regie_home)
    store.record_quota(CLAUDE, AgentResult(
        outcome="quota", quota_kind="session", quota_scope="provider",
        quota_reset_at="2999-01-01T00:00:00+00:00"))

    paid = Binding(cli="claude", model="opus", auth="api")
    assert store.reserve(paid).allowed is True


def test_expired_circuit_allows_one_half_open_probe(regie_home):
    store = ProviderHealthStore(regie_home)
    reset = "2026-08-14T09:00:00+00:00"
    store.record_quota(CLAUDE, AgentResult(
        outcome="quota", quota_kind="session", quota_scope="provider",
        quota_reset_at=reset))
    now = datetime(2026, 8, 14, 9, 1, tzinfo=UTC)
    first = store.reserve(CLAUDE, now=now)
    second = store.reserve(CLAUDE, now=now + timedelta(seconds=1))
    assert first.allowed and first.probe
    assert not second.allowed and "probe" in second.reason

    store.finish_probe(CLAUDE, first, AgentResult(outcome="done"), now=now)
    assert store.reserve(CLAUDE, now=now).allowed
    assert store.entries() == {}


def test_dispatch_skips_globally_unavailable_provider_without_spawning(
        regie_home, tmp_path):
    rd = RunDir.create(regie_home, "r1")
    store = ProviderHealthStore(regie_home)
    store.record_quota(CLAUDE, AgentResult(
        outcome="quota", text="weekly limit", quota_kind="weekly",
        quota_scope="provider", quota_reset_at="2999-01-01T00:00:00+00:00"))
    req = AgentRequest(prompt="do it", cwd=tmp_path, binding=CLAUDE,
                       budgets=Budgets())

    result = run_agent(rd, "T1", "build", 1, req)

    assert result.outcome == "quota" and result.quota_synthetic
    transcript = rd.path / "tasks" / "T1" / "attempt-1.out"
    assert "unavailable" in transcript.read_text()
    assert rd.read_intents()[0]["binding"]["cli"] == "claude"


def test_provider_status_and_manual_reset(regie_home):
    from regie.cli import app

    ProviderHealthStore(regie_home).record_quota(CLAUDE, AgentResult(
        outcome="quota", quota_kind="weekly", quota_scope="provider",
        quota_reset_at="2999-01-01T00:00:00+00:00"))
    runner = CliRunner()

    status = runner.invoke(app, ["provider-status"])
    assert status.exit_code == 0
    assert "claude:subscription" in status.output and "unavailable" in status.output

    cleared = runner.invoke(app, ["provider-reset", "claude", "--auth", "subscription"])
    assert cleared.exit_code == 0 and "cleared 1" in cleared.output
    assert runner.invoke(app, ["provider-status"]).output == "all providers available\n"
