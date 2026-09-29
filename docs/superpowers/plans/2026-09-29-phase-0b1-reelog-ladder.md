# Phase 0b-1 — Reelog Feature Ladder (fixture + first three cases) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a realistic FastAPI + Next.js fixture ("Reelog", a film and series catalog with a watchlist) to regie-bench, three validated ladder cases of rising scope on it, and the first empirical-difficulty measurement, so Phase 0b-2 can size the remaining cases from evidence.

**Architecture:** The fixture lives in `regie-bench/fixtures/reelog/` as a small monorepo: `backend/` (FastAPI, SQLAlchemy 2, Alembic, SQLite, pytest, uv) and `frontend/` (Next.js App Router, TypeScript, vitest + Testing Library in jsdom, ESLint). It carries deliberate, realistic debt: a catalog "god" service, N+1 queries, duplicated fetch code, and a pagination bug. Each case in `cases/reelog/<case>/` ships a stakeholder-style brief, a hidden intent document, self-contained hidden tests (Phase 0a's `hidden-tests` evaluator), and a `reference.patch` proven by `regie-bench validate`. The bench also learns to exclude installed artifacts from fixture copies and fingerprints, to fingerprint the fixture itself, and to run fixture setup during smoke.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, Alembic, pytest, uv; Next.js 15, React 19, TypeScript 5, vitest 3, jsdom, Testing Library, ESLint 9 + typescript-eslint 8; regie-bench (stdlib).

**Spec:** `docs/superpowers/specs/2026-09-28-regie-agile-framework-design.md` (§6.2 feature ladder, §6.3 reference solutions, §6.5 empirical difficulty, §8 Phase 0b). Machinery from `docs/superpowers/plans/2026-09-28-phase-0a-bench-machinery.md`.

**Scope:** this plan is the first half of Phase 0b. Plan 0b-2, written after Task 8's measurement, adds the ambiguous request, larger cross-layer and multi-increment features, the greenfield case, and the two holdout cases, sized from this plan's evidence.

## Global Constraints

- All work is in `/Users/frederic/Code/regie-bench`; commit on `main`, never push.
- Every commit message ends with:
  ```
  Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01RbtkGAHGVyLqrM25Tdxw2w
  ```
- regie-bench keeps `dependencies = []` (standard library only at runtime); ruff line length 100.
- A fixture never ships installed or generated artifacts: `node_modules`, `.venv`, `.next`, `__pycache__`, `.pytest_cache`, `.ruff_cache`, `*.tsbuildinfo`, `*.pyc`, `*.db`.
- Fixture Python: `>=3.12` through uv (`backend/.python-version` = `3.12`). Node: `>=20` (CI uses 24).
- Frontend majors (exact versions resolved and locked at install): `next@15 react@19 react-dom@19`, dev `typescript@5 @types/node@22 @types/react@19 @types/react-dom@19 vitest@3 @vitejs/plugin-react@4 jsdom@26 @testing-library/react@16 @testing-library/dom@10 eslint@9 typescript-eslint@8`.
- Hidden-test commands never install or sync dependencies: backend `uv run --frozen --no-sync`, frontend `npx --no-install`.
- Hidden tests live outside the fixture's own test directories (`backend/bench_hidden/`, `frontend/bench_hidden/`) and carry their own `conftest.py`.
- Verification (regie-bench): `uv run pytest -q`, `uv run ruff check src tests`, `uv run regie-bench smoke`, `uv run regie-bench validate`.

## Review Focus

1. After fixture setup and every gate run in a fresh trial, `git status --porcelain` must be empty: installed dependencies and caches must never be committed by Régie. (Task 4)
2. A trial's copy of the fixture must never include the author's installed `node_modules`/`.venv`; it must install from the lockfiles. (Task 1)
3. Hidden tests must pass on the reference solution without the fixture's public tests or conftest (an agent may rewrite those). (Task 5)
4. The runtime migration must be judged against a database created at revision `0001` with existing rows, not a fresh database. (Task 6)
5. Query counting must observe every engine the app creates, independent of `app.state` internals. (Task 7)

---

### Task 1: Bench — clean fixture copies, fixture fingerprint, setup during smoke

**Files:**
- Modify: `src/regie_bench/acceptance.py` (`FIXTURE_IGNORES`, `content_files`, `payload_files`, `fingerprint`)
- Modify: `src/regie_bench/cli.py` (`prepare_case`, `evaluate_trial`, `case_difficulty`, `smoke`)
- Modify: `tests/minibench.py` (`add_case` gains `outcome=`)
- Modify: `.github/workflows/ci.yml` (npm cache)
- Test: `tests/test_acceptance.py`, `tests/test_cli.py`

**Interfaces:**
- Produces: `FIXTURE_IGNORES: tuple[str, ...]`; `content_files(directory: Path) -> list[Path]` (sorted, prunes ignored names); `fingerprint(brief, evaluator, fixture: Path | None = None, overlay: Path | None = None)` → adds `"fixture_sha256": str | None`; `add_case(root, case_id, manifest, files=None, *, outcome="completed")`.

- [ ] **Step 1: Write the failing tests**

In `tests/minibench.py`, replace `add_case`:

```python
def add_case(root: Path, case_id: str, manifest: str,
             files: dict[str, str] | None = None, *, outcome: str = "completed") -> Path:
    """Write cases/mini/<case_id>; `manifest` is appended to the required keys."""
    case_dir = root / "cases" / "mini" / case_id
    write(case_dir / "case.toml",
          f'id = "{case_id}"\nfixture = "mini"\ndescription = "test case"\n'
          f'expected_outcome = "{outcome}"\n{manifest}')
    write(case_dir / "brief.md", "Set value.txt to done.\n")
    for relative, text in (files or {}).items():
        write(case_dir / relative, text)
    return case_dir
```

Append to `tests/test_acceptance.py` (import `content_files` from `regie_bench.acceptance`):

```python
def test_fixture_fingerprint_follows_content_and_ignores_installed_artifacts(tmp_path):
    root = build_bench(tmp_path)
    add_case(root, "mini", SCRIPT_EVALUATOR, {"check.py": DONE_SCRIPT})
    case = load_cases(root)["mini"]
    fixture = case.fixture_dir
    first = fingerprint(case.brief, case.evaluator, fixture)["fixture_sha256"]

    (fixture / "node_modules" / "pkg").mkdir(parents=True)
    (fixture / "node_modules" / "pkg" / "index.js").write_text("installed\n")
    (fixture / "app.tsbuildinfo").write_text("{}\n")
    unchanged = fingerprint(case.brief, case.evaluator, fixture)["fixture_sha256"]
    (fixture / "value.txt").write_text("edited\n")
    edited = fingerprint(case.brief, case.evaluator, fixture)["fixture_sha256"]

    assert unchanged == first
    assert edited != first
    assert fingerprint(case.brief, case.evaluator)["fixture_sha256"] is None
    assert [p.name for p in content_files(fixture)] == [
        "check_public.py", "regie.toml", "value.txt"]
```

Append to `tests/test_cli.py` (add `PYTHON` to the `minibench` import and `smoke` to the `regie_bench.cli` import):

```python
def test_trial_copies_leave_installed_artifacts_behind(tmp_path):
    root = build_bench(tmp_path)
    add_case(root, "mini-done", SCRIPT_EVALUATOR, {"check.py": DONE_SCRIPT})
    fixture = root / "fixtures" / "mini"
    (fixture / "node_modules").mkdir()
    (fixture / "node_modules" / "left.js").write_text("x\n")
    (fixture / ".venv").mkdir()

    trial = prepare_case(load_cases(root)["mini-done"], tmp_path / "results", "codex",
                         trial_id="t1")

    assert not (trial / "repo" / "node_modules").exists()
    assert not (trial / "repo" / ".venv").exists()


def _setup_bench(tmp_path):
    root = build_bench(tmp_path)
    fixture = root / "fixtures" / "mini"
    (fixture / "setup_env.py").write_text("open('ready', 'w').close()\n")
    (fixture / "check_ready.py").write_text(
        "import pathlib, sys\nsys.exit(0 if pathlib.Path('ready').exists() else 1)\n")
    (fixture / "regie.toml").write_text(
        '[providers]\nenabled = ["codex"]\n\n[commands]\n'
        f'setup = "{PYTHON} setup_env.py"\n'
        f'test = "{PYTHON} check_ready.py && {PYTHON} check_public.py"\n'
        f'lint = "{PYTHON} -c pass"\n')
    return root


def test_smoke_runs_setup_before_the_quality_gates(tmp_path):
    root = _setup_bench(tmp_path)
    add_case(root, "mini-done", SCRIPT_EVALUATOR, {"check.py": DONE_SCRIPT})

    assert smoke(load_cases(root), tmp_path / "results") == 0


def test_smoke_skips_setup_for_infrastructure_cases(tmp_path):
    root = _setup_bench(tmp_path)
    overlay = (
        '[providers]\nenabled = ["codex"]\n\n[commands]\n'
        'setup = "exit 7"\n'
        f'test = "{PYTHON} check_public.py"\n')
    add_case(root, "mini-infra", "", {"overlay/regie.toml": overlay},
             outcome="infrastructure_halt")

    assert smoke(load_cases(root), tmp_path / "results") == 0


def test_difficulty_warns_when_trials_ran_on_different_fixtures():
    records = [_trial(True, 1, fixture_sha256="f1"), _trial(True, 1, fixture_sha256="f1"),
               _trial(True, 1, fixture_sha256="f2")]

    assert case_difficulty(records)["warnings"] == [
        "trials were judged by different briefs or evaluators"]
```

Replace `test_result_records_mode_and_fingerprints`'s last assertion with:

```python
    assert {key: result[key] for key in ("brief_sha256", "evaluator_sha256",
                                         "fixture_sha256")} == (
        fingerprint(case.brief, case.evaluator, case.fixture_dir, case.overlay))
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest -q tests/test_acceptance.py tests/test_cli.py -k "installed_artifacts or runs_setup or skips_setup or different_fixtures or records_mode"`
Expected: FAIL — `ImportError: cannot import name 'content_files'` (test_acceptance) and, in test_cli, a copied `node_modules`, a smoke failure on the missing `ready` marker, a missing fixture warning, and a `KeyError: 'fixture_sha256'`. `test_smoke_skips_setup_for_infrastructure_cases` already passes (smoke never ran setup); it guards the new setup step.

- [ ] **Step 3: Implement** — in `src/regie_bench/acceptance.py` (add `from fnmatch import fnmatch`):

```python
# Installed or generated artifacts: never part of a fixture or of hidden tests.
FIXTURE_IGNORES = ("node_modules", ".venv", ".next", "__pycache__", ".pytest_cache",
                   ".ruff_cache", "*.tsbuildinfo", "*.pyc", "*.db")


def _ignored(name: str) -> bool:
    return any(fnmatch(name, pattern) for pattern in FIXTURE_IGNORES)


def content_files(directory: Path) -> list[Path]:
    """Authored files under `directory`, pruning installed and generated ones."""
    files: list[Path] = []
    for root, dirs, names in os.walk(directory):
        dirs[:] = [name for name in dirs if not _ignored(name)]
        files.extend(Path(root) / name for name in names if not _ignored(name))
    return sorted(files)
```

Replace the body of `payload_files` with `return content_files(directory)` (keep its docstring). Replace `fingerprint`:

```python
def fingerprint(brief: Path, evaluator: Evaluator | None, fixture: Path | None = None,
                overlay: Path | None = None) -> dict[str, str | None]:
    """Content hashes: two results are comparable only when the brief, the
    fixture they started from, and the hidden acceptance that judged them are
    identical."""
    if evaluator is None:
        evaluator_sha = None
    elif evaluator.kind == "script":
        evaluator_sha = _digest([evaluator.path], evaluator.path.parent,
                                "\0".join(evaluator.command))
    else:
        evaluator_sha = _digest(payload_files(evaluator.path), evaluator.path,
                                "\0".join(evaluator.commands))
    fixture_sha = None
    if fixture is not None:
        digest = hashlib.sha256()
        for base, prefix in ((fixture, "fixture"), (overlay, "overlay")):
            if base is None:
                continue
            for path in content_files(base):
                digest.update(f"\0{prefix}/{path.relative_to(base).as_posix()}\0".encode())
                digest.update(path.read_bytes())
        fixture_sha = digest.hexdigest()
    return {
        "brief_sha256": hashlib.sha256(brief.read_bytes()).hexdigest(),
        "evaluator_sha256": evaluator_sha,
        "fixture_sha256": fixture_sha,
    }
```

In `src/regie_bench/cli.py` (import `FIXTURE_IGNORES` from `regie_bench.acceptance`):

- `prepare_case`: `shutil.copytree(case.fixture_dir, repo, ignore=shutil.ignore_patterns(*FIXTURE_IGNORES))`.
- `evaluate_trial`: `**fingerprint(case.brief, case.evaluator, case.fixture_dir, case.overlay),`.
- `case_difficulty`: the comparability set becomes
  `{(r.get("brief_sha256"), r.get("evaluator_sha256"), r.get("fixture_sha256")) for r in baseline}`.
- `smoke`: run setup first (skipped for infrastructure cases, whose setup is meant to fail) and run every command through the shell:

```python
def smoke(cases: dict[str, Case], results_root: Path) -> int:
    failures = 0
    for case in cases.values():
        trial = prepare_case(case, results_root, "codex")
        repo = trial / "repo"
        configured = tomllib.loads((repo / "regie.toml").read_text())["commands"]
        outputs = []
        case_passed = True
        # Infrastructure cases break setup on purpose; Régie's trial checks
        # that it halts. Everywhere else the gates need their dependencies.
        names = ("test", "lint", "build")
        if case.expected_outcome != "infrastructure_halt":
            names = ("setup", *names)
        for name in names:
            if name not in configured:
                continue
            result = _run(["/bin/sh", "-c", configured[name]], repo, check=False)
            outputs.append(f"$ {configured[name]}\n{result.stdout}")
            case_passed = case_passed and result.returncode == 0
        (trial / "smoke.log").write_text("\n".join(outputs))
        failures += not case_passed
        verdict = "PASS" if case_passed else "FAIL"
        print(f"{verdict:4} {case.id}")
    return 1 if failures else 0
```

Remove the now-unused `import shlex` if ruff reports it.

In `.github/workflows/ci.yml`, give the Node setup a cache:

```yaml
      - uses: actions/setup-node@v4
        with:
          node-version: 24
          cache: npm
          cache-dependency-path: fixtures/**/package-lock.json
```

(Until Task 3 adds `fixtures/reelog/frontend/package-lock.json`, `fixtures/taskboard/package-lock.json` matches the glob.)

- [ ] **Step 4: Run the verification**

Run: `uv run pytest -q && uv run ruff check src tests && uv run regie-bench smoke`
Expected: all pass; `All checks passed!`; every smoke case `PASS`.

- [ ] **Step 5: Commit**

```bash
git add src/regie_bench/acceptance.py src/regie_bench/cli.py tests/minibench.py tests/test_acceptance.py tests/test_cli.py .github/workflows/ci.yml
git commit -F - <<'EOF'
feat: fingerprint fixtures, copy them clean, and run setup in smoke

Fixture copies and fingerprints skip installed and generated artifacts,
results record fixture_sha256, and smoke installs dependencies before the
gates (except for infrastructure cases, whose setup fails on purpose).

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RbtkGAHGVyLqrM25Tdxw2w
EOF
```

---

### Task 2: Reelog backend

**Files (all under `fixtures/reelog/`):**
- Create: `.gitignore`, `backend/.python-version`, `backend/pyproject.toml`, `backend/uv.lock` (generated), `backend/alembic.ini`, `backend/migrations/env.py`, `backend/migrations/script.py.mako`, `backend/migrations/versions/0001_initial.py`
- Create: `backend/app/__init__.py`, `backend/app/db.py`, `backend/app/models.py`, `backend/app/migrate.py`, `backend/app/deps.py`, `backend/app/schemas.py`, `backend/app/main.py`, `backend/app/seed.py`, `backend/app/services/__init__.py`, `backend/app/services/catalog.py`, `backend/app/routers/__init__.py`, `backend/app/routers/titles.py`, `backend/app/routers/tags.py`, `backend/app/routers/watchlist.py`
- Test: `backend/tests/conftest.py`, `backend/tests/test_titles.py`, `backend/tests/test_watchlist.py`, `backend/tests/test_tags.py`, `backend/tests/test_migrations.py`

**Interfaces:**
- Produces (stable, used by hidden tests): `app.main.create_app(database_url: str | None = None) -> FastAPI`; `app.migrate.upgrade(database_url: str, revision: str = "head") -> None`; Alembic revision `"0001"`; `app.models.User(name, api_key)`; `app.state.sessions` (a `sessionmaker`); header `X-API-Key`.
- Produces (HTTP): `GET/POST /api/titles` (list: `page`, `page_size`, `q`; body `Page`), `GET/PUT/DELETE /api/titles/{id}`, `GET/POST /api/titles/{id}/episodes`, `GET /api/tags`, `GET /api/watchlist`, `PUT/DELETE /api/watchlist/{id}`. Title JSON: `{id, name, kind, year, synopsis, tags, episode_count}`; Page JSON: `{items, page, page_size, total, total_pages, has_next}`.
- Deliberate debt (keep it): `app/services/catalog.py` holds every catalog concern; listing lazily loads tags and episodes per title (N+1); `total_pages` uses floor division, so a partial last page is unreachable; the watchlist loads each title separately; `_title_or_404` is duplicated across routers.

- [ ] **Step 1: Scaffold the project and lock dependencies**

`fixtures/reelog/.gitignore`:

```gitignore
backend/.venv/
frontend/node_modules/
frontend/.next/
*.tsbuildinfo
__pycache__/
.pytest_cache/
.ruff_cache/
*.db
```

`fixtures/reelog/backend/.python-version`:

```
3.12
```

`fixtures/reelog/backend/pyproject.toml`:

```toml
[project]
name = "reelog-backend"
version = "0.1.0"
description = "Reelog API"
requires-python = ">=3.12"
dependencies = [
    "alembic>=1.13,<2",
    "fastapi>=0.115,<1",
    "pydantic>=2.7,<3",
    "sqlalchemy>=2.0,<2.2",
    "uvicorn>=0.30,<1",
]

[dependency-groups]
dev = [
    "httpx>=0.27,<1",
    "pytest>=8,<10",
    "ruff>=0.6,<1",
]

[tool.pytest.ini_options]
pythonpath = ["."]
testpaths = ["tests"]

[tool.ruff]
line-length = 100
```

Run: `cd fixtures/reelog/backend && uv lock && uv sync --frozen`
Expected: `uv.lock` written; a `.venv` created (git-ignored).

- [ ] **Step 2: Write the failing tests**

`backend/tests/conftest.py`:

```python
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.migrate import upgrade
from app.models import User


@pytest.fixture
def database_url(tmp_path) -> str:
    url = f"sqlite:///{tmp_path / 'test.db'}"
    upgrade(url)
    return url


@pytest.fixture
def client(database_url):
    with TestClient(create_app(database_url)) as test_client:
        yield test_client


@pytest.fixture
def auth(client) -> dict[str, str]:
    with client.app.state.sessions() as session:
        session.add(User(name="Ada", api_key="test-key"))
        session.commit()
    return {"X-API-Key": "test-key"}


@pytest.fixture
def make_title(client, auth):
    def make(**fields):
        body = {"name": "Arrival", "kind": "film", "year": 2016, "synopsis": "", "tags": [],
                **fields}
        response = client.post("/api/titles", json=body, headers=auth)
        assert response.status_code == 201, response.text
        return response.json()
    return make
```

`backend/tests/test_titles.py`:

```python
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.models import Episode


def test_a_title_can_be_created_and_fetched(client, make_title):
    created = make_title(name="Arrival", year=2016, synopsis="Linguists.", tags=["sci-fi"])

    fetched = client.get(f"/api/titles/{created['id']}").json()

    assert fetched == {"id": created["id"], "name": "Arrival", "kind": "film", "year": 2016,
                       "synopsis": "Linguists.", "tags": ["sci-fi"], "episode_count": 0}


def test_tags_are_trimmed_lowercased_deduplicated_and_sorted(make_title):
    title = make_title(tags=["Sci-Fi ", "drama", "DRAMA", " "])

    assert title["tags"] == ["drama", "sci-fi"]


@pytest.mark.parametrize("field,value", [("year", 1700), ("name", ""), ("kind", "book")])
def test_invalid_titles_are_rejected(client, auth, field, value):
    body = {"name": "Arrival", "kind": "film", "year": 2016, field: value}

    assert client.post("/api/titles", json=body, headers=auth).status_code == 422


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}])
def test_writes_require_a_valid_api_key(client, headers):
    body = {"name": "Arrival", "kind": "film", "year": 2016}

    assert client.post("/api/titles", json=body, headers=headers).status_code == 401


def test_listing_is_sorted_by_name_and_searchable(client, make_title):
    for name in ("Dune", "Arrival", "Dune: Part Two"):
        make_title(name=name)

    listed = client.get("/api/titles").json()
    searched = client.get("/api/titles", params={"q": "DUNE"}).json()

    assert [item["name"] for item in listed["items"]] == ["Arrival", "Dune", "Dune: Part Two"]
    assert [item["name"] for item in searched["items"]] == ["Dune", "Dune: Part Two"]
    assert searched["total"] == 2


def test_pagination_splits_an_exact_multiple_into_full_pages(client, make_title):
    for index in range(20):
        make_title(name=f"Title {index:02d}")

    first = client.get("/api/titles", params={"page": 1, "page_size": 10}).json()
    second = client.get("/api/titles", params={"page": 2, "page_size": 10}).json()

    assert (first["total_pages"], first["has_next"]) == (2, True)
    assert (second["total_pages"], second["has_next"]) == (2, False)
    assert [item["name"] for item in second["items"]] == [f"Title {i:02d}" for i in range(10, 20)]


def test_a_title_can_be_replaced(client, auth, make_title):
    title = make_title(name="Arival", tags=["drama"])
    body = {"name": "Arrival", "kind": "film", "year": 2016, "synopsis": "Fixed.", "tags": []}

    updated = client.put(f"/api/titles/{title['id']}", json=body, headers=auth).json()

    assert (updated["name"], updated["synopsis"], updated["tags"]) == ("Arrival", "Fixed.", [])


def test_deleting_a_series_removes_its_episodes(client, auth, make_title):
    series = make_title(name="Dark", kind="series")
    client.post(f"/api/titles/{series['id']}/episodes",
                json={"season": 1, "number": 1, "name": "Secrets"}, headers=auth)

    assert client.delete(f"/api/titles/{series['id']}", headers=auth).status_code == 204
    assert client.get(f"/api/titles/{series['id']}").status_code == 404
    with client.app.state.sessions() as session:
        assert session.scalar(select(func.count(Episode.id))) == 0


def test_series_episodes_are_listed_in_order_and_counted(client, auth, make_title):
    series = make_title(name="Dark", kind="series")
    for season, number in ((2, 1), (1, 2), (1, 1)):
        response = client.post(f"/api/titles/{series['id']}/episodes",
                               json={"season": season, "number": number,
                                     "name": f"{season}x{number}"},
                               headers=auth)
        assert response.status_code == 201

    episodes = client.get(f"/api/titles/{series['id']}/episodes").json()

    assert [(e["season"], e["number"]) for e in episodes] == [(1, 1), (1, 2), (2, 1)]
    assert client.get(f"/api/titles/{series['id']}").json()["episode_count"] == 3


def test_films_have_no_episodes(client, auth, make_title):
    film = make_title(kind="film")

    response = client.post(f"/api/titles/{film['id']}/episodes",
                           json={"season": 1, "number": 1, "name": "Pilot"}, headers=auth)

    assert response.status_code == 422


def test_an_episode_cannot_be_added_twice(client, auth, make_title):
    series = make_title(kind="series")
    episode = {"season": 1, "number": 1, "name": "Pilot"}
    client.post(f"/api/titles/{series['id']}/episodes", json=episode, headers=auth)

    duplicate = client.post(f"/api/titles/{series['id']}/episodes", json=episode, headers=auth)

    assert duplicate.status_code == 409


def test_a_missing_title_is_not_found(client):
    assert client.get("/api/titles/999").status_code == 404
```

`backend/tests/test_watchlist.py`:

```python
from __future__ import annotations


def test_adding_to_the_watchlist_is_idempotent(client, auth, make_title):
    title = make_title(name="Arrival")
    for _ in range(2):
        assert client.put(f"/api/watchlist/{title['id']}", headers=auth).status_code == 204

    items = client.get("/api/watchlist", headers=auth).json()

    assert [item["title"]["name"] for item in items] == ["Arrival"]


def test_a_title_can_be_removed_from_the_watchlist(client, auth, make_title):
    title = make_title()
    client.put(f"/api/watchlist/{title['id']}", headers=auth)

    assert client.delete(f"/api/watchlist/{title['id']}", headers=auth).status_code == 204
    assert client.get("/api/watchlist", headers=auth).json() == []


def test_the_watchlist_is_private(client):
    assert client.get("/api/watchlist").status_code == 401


def test_watching_a_missing_title_is_not_found(client, auth):
    assert client.put("/api/watchlist/999", headers=auth).status_code == 404
```

`backend/tests/test_tags.py`:

```python
from __future__ import annotations


def test_tags_report_how_many_titles_use_them(client, make_title):
    make_title(name="Arrival", tags=["sci-fi", "drama"])
    make_title(name="Dune", tags=["sci-fi"])

    assert client.get("/api/tags").json() == [{"name": "drama", "count": 1},
                                              {"name": "sci-fi", "count": 2}]
```

`backend/tests/test_migrations.py`:

```python
from __future__ import annotations

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from app import models  # noqa: F401 - registers every table on Base.metadata
from app.db import Base


def test_migrations_match_the_models(database_url):
    engine = create_engine(database_url)
    with engine.connect() as connection:
        differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    engine.dispose()

    assert differences == [], "add an Alembic migration for these model changes"
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd fixtures/reelog/backend && uv run --frozen pytest -q`
Expected: collection errors — `ModuleNotFoundError: No module named 'app'`.

- [ ] **Step 4: Implement the backend**

`backend/alembic.ini`:

```ini
[alembic]
script_location = migrations
prepend_sys_path = .
sqlalchemy.url = sqlite:///reelog.db
```

`backend/migrations/env.py`:

```python
from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from app import models  # noqa: F401 - registers every table on Base.metadata
from app.db import Base

config = context.config
target_metadata = Base.metadata


def run_migrations() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


run_migrations()
```

`backend/migrations/script.py.mako`:

```mako
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
```

`backend/migrations/versions/0001_initial.py`:

```python
"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("api_key", sa.String(64), nullable=False, unique=True),
    )
    op.create_table(
        "tags",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(40), nullable=False, unique=True),
    )
    op.create_table(
        "titles",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("year", sa.Integer, nullable=False),
        sa.Column("synopsis", sa.Text, nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_table(
        "title_tags",
        sa.Column("title_id", sa.Integer, sa.ForeignKey("titles.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("tag_id", sa.Integer, sa.ForeignKey("tags.id", ondelete="CASCADE"),
                  primary_key=True),
    )
    op.create_table(
        "episodes",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("title_id", sa.Integer, sa.ForeignKey("titles.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("season", sa.Integer, nullable=False),
        sa.Column("number", sa.Integer, nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.UniqueConstraint("title_id", "season", "number"),
    )
    op.create_table(
        "watchlist_entries",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("title_id", sa.Integer, sa.ForeignKey("titles.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("added_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint("user_id", "title_id"),
    )


def downgrade() -> None:
    for table in ("watchlist_entries", "episodes", "title_tags", "titles", "tags", "users"):
        op.drop_table(table)
```

`backend/app/__init__.py`: empty file. `backend/app/services/__init__.py` and `backend/app/routers/__init__.py`: empty files.

`backend/app/db.py`:

```python
from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def make_engine(database_url: str) -> Engine:
    engine = create_engine(database_url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _record) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    return engine


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)
```

`backend/app/models.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Table, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


title_tags = Table(
    "title_tags",
    Base.metadata,
    Column("title_id", ForeignKey("titles.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    api_key: Mapped[str] = mapped_column(String(64), unique=True)


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(40), unique=True)


class Title(Base):
    __tablename__ = "titles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(10))  # "film" or "series"
    year: Mapped[int] = mapped_column(Integer)
    synopsis: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    tags: Mapped[list[Tag]] = relationship(secondary=title_tags)
    episodes: Mapped[list[Episode]] = relationship(
        back_populates="title", cascade="all, delete-orphan", passive_deletes=True)


class Episode(Base):
    __tablename__ = "episodes"
    __table_args__ = (UniqueConstraint("title_id", "season", "number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    title_id: Mapped[int] = mapped_column(ForeignKey("titles.id", ondelete="CASCADE"))
    season: Mapped[int] = mapped_column(Integer)
    number: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(200))
    title: Mapped[Title] = relationship(back_populates="episodes")


class WatchlistEntry(Base):
    __tablename__ = "watchlist_entries"
    __table_args__ = (UniqueConstraint("user_id", "title_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title_id: Mapped[int] = mapped_column(ForeignKey("titles.id", ondelete="CASCADE"))
    added_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
```

`backend/app/migrate.py`:

```python
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

BACKEND = Path(__file__).resolve().parents[1]


def alembic_config(database_url: str) -> Config:
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def upgrade(database_url: str, revision: str = "head") -> None:
    """Migrate `database_url` to `revision`. Tooling depends on this signature."""
    command.upgrade(alembic_config(database_url), revision)
```

`backend/app/deps.py`:

```python
from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import User


def get_session(request: Request) -> Iterator[Session]:
    session = request.app.state.sessions()
    try:
        yield session
    finally:
        session.close()


def current_user(x_api_key: str | None = Header(default=None),
                 session: Session = Depends(get_session)) -> User:
    if not x_api_key:
        raise HTTPException(status_code=401, detail="missing X-API-Key header")
    user = session.scalar(select(User).where(User.api_key == x_api_key))
    if user is None:
        raise HTTPException(status_code=401, detail="unknown API key")
    return user
```

`backend/app/schemas.py`:

```python
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TitleIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["film", "series"]
    year: int = Field(ge=1888, le=2100)
    synopsis: str = ""
    tags: list[str] = Field(default_factory=list)


class TitleOut(BaseModel):
    id: int
    name: str
    kind: Literal["film", "series"]
    year: int
    synopsis: str
    tags: list[str]
    episode_count: int


class Page(BaseModel):
    items: list[TitleOut]
    page: int
    page_size: int
    total: int
    total_pages: int
    has_next: bool


class EpisodeIn(BaseModel):
    season: int = Field(ge=1)
    number: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=200)


class EpisodeOut(EpisodeIn):
    model_config = ConfigDict(from_attributes=True)

    id: int


class TagCount(BaseModel):
    name: str
    count: int


class WatchlistItem(BaseModel):
    title: TitleOut
    added_at: str
```

`backend/app/services/catalog.py`:

```python
"""Catalog queries and serialization.

Everything catalog-related grew up here: listing, search, pagination, tags,
episodes, and the watchlist.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import Episode, Tag, Title, User, WatchlistEntry, title_tags

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


class CatalogError(ValueError):
    """A request that breaks a catalog rule; `status_code` is the HTTP answer."""

    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


def serialize_title(title: Title) -> dict[str, Any]:
    return {
        "id": title.id,
        "name": title.name,
        "kind": title.kind,
        "year": title.year,
        "synopsis": title.synopsis,
        "tags": sorted(tag.name for tag in title.tags),
        "episode_count": len(title.episodes),
    }


def list_titles(session: Session, *, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE,
                search: str | None = None) -> dict[str, Any]:
    page = max(1, page)
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))
    query = select(Title)
    count_query = select(func.count(Title.id))
    if search:
        pattern = f"%{search.strip().lower()}%"
        query = query.where(func.lower(Title.name).like(pattern))
        count_query = count_query.where(func.lower(Title.name).like(pattern))
    total = session.scalar(count_query) or 0
    titles = session.scalars(
        query.order_by(Title.name, Title.id).offset((page - 1) * page_size).limit(page_size)
    ).all()
    total_pages = max(1, total // page_size)
    return {
        "items": [serialize_title(title) for title in titles],
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        "has_next": page < total_pages,
    }


def get_title(session: Session, title_id: int) -> Title | None:
    return session.get(Title, title_id)


def _tags(session: Session, names: list[str]) -> list[Tag]:
    tags = []
    for name in sorted({name.strip().lower() for name in names if name.strip()}):
        tag = session.scalar(select(Tag).where(Tag.name == name))
        if tag is None:
            tag = Tag(name=name)
            session.add(tag)
        tags.append(tag)
    return tags


def _apply(session: Session, title: Title, data: dict[str, Any]) -> None:
    title.name = data["name"].strip()
    title.kind = data["kind"]
    title.year = data["year"]
    title.synopsis = data.get("synopsis", "")
    title.tags = _tags(session, data.get("tags", []))


def create_title(session: Session, data: dict[str, Any]) -> Title:
    title = Title()
    _apply(session, title, data)
    session.add(title)
    session.commit()
    return title


def update_title(session: Session, title: Title, data: dict[str, Any]) -> Title:
    if title.kind == "series" and data["kind"] == "film" and title.episodes:
        raise CatalogError("a series with episodes cannot become a film", 409)
    _apply(session, title, data)
    session.commit()
    return title


def delete_title(session: Session, title: Title) -> None:
    session.delete(title)
    session.commit()


def list_tags(session: Session) -> list[dict[str, Any]]:
    rows = session.execute(
        select(Tag.name, func.count(title_tags.c.title_id))
        .outerjoin(title_tags, title_tags.c.tag_id == Tag.id)
        .group_by(Tag.id)
        .order_by(Tag.name)
    ).all()
    return [{"name": name, "count": count} for name, count in rows]


def list_episodes(session: Session, title: Title) -> list[Episode]:
    return list(session.scalars(
        select(Episode).where(Episode.title_id == title.id)
        .order_by(Episode.season, Episode.number)
    ))


def add_episode(session: Session, title: Title, data: dict[str, Any]) -> Episode:
    if title.kind != "series":
        raise CatalogError("only series have episodes")
    duplicate = session.scalar(select(Episode).where(
        Episode.title_id == title.id, Episode.season == data["season"],
        Episode.number == data["number"]))
    if duplicate is not None:
        raise CatalogError("that episode already exists", 409)
    episode = Episode(title_id=title.id, **data)
    session.add(episode)
    session.commit()
    return episode


def watchlist(session: Session, user: User) -> list[dict[str, Any]]:
    entries = session.scalars(
        select(WatchlistEntry).where(WatchlistEntry.user_id == user.id)
        .order_by(WatchlistEntry.added_at, WatchlistEntry.id)
    ).all()
    return [{"title": serialize_title(session.get(Title, entry.title_id)),
             "added_at": entry.added_at.isoformat()} for entry in entries]


def add_to_watchlist(session: Session, user: User, title: Title) -> None:
    exists = session.scalar(select(WatchlistEntry).where(
        WatchlistEntry.user_id == user.id, WatchlistEntry.title_id == title.id))
    if exists is None:
        session.add(WatchlistEntry(user_id=user.id, title_id=title.id))
        session.commit()


def remove_from_watchlist(session: Session, user: User, title: Title) -> None:
    session.execute(delete(WatchlistEntry).where(
        WatchlistEntry.user_id == user.id, WatchlistEntry.title_id == title.id))
    session.commit()
```

`backend/app/routers/titles.py`:

```python
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.deps import current_user, get_session
from app.models import Title, User
from app.schemas import EpisodeIn, EpisodeOut, Page, TitleIn, TitleOut
from app.services import catalog

router = APIRouter(prefix="/api/titles", tags=["titles"])


def _title_or_404(session: Session, title_id: int) -> Title:
    title = catalog.get_title(session, title_id)
    if title is None:
        raise HTTPException(status_code=404, detail="title not found")
    return title


@router.get("", response_model=Page)
def list_titles(page: int = 1, page_size: int = catalog.DEFAULT_PAGE_SIZE,
                q: str | None = None, session: Session = Depends(get_session)):
    return catalog.list_titles(session, page=page, page_size=page_size, search=q)


@router.post("", response_model=TitleOut, status_code=201)
def create_title(data: TitleIn, session: Session = Depends(get_session),
                 _user: User = Depends(current_user)):
    return catalog.serialize_title(catalog.create_title(session, data.model_dump()))


@router.get("/{title_id}", response_model=TitleOut)
def get_title(title_id: int, session: Session = Depends(get_session)):
    return catalog.serialize_title(_title_or_404(session, title_id))


@router.put("/{title_id}", response_model=TitleOut)
def update_title(title_id: int, data: TitleIn, session: Session = Depends(get_session),
                 _user: User = Depends(current_user)):
    title = _title_or_404(session, title_id)
    try:
        catalog.update_title(session, title, data.model_dump())
    except catalog.CatalogError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return catalog.serialize_title(title)


@router.delete("/{title_id}", status_code=204)
def delete_title(title_id: int, session: Session = Depends(get_session),
                 _user: User = Depends(current_user)):
    catalog.delete_title(session, _title_or_404(session, title_id))
    return Response(status_code=204)


@router.get("/{title_id}/episodes", response_model=list[EpisodeOut])
def list_episodes(title_id: int, session: Session = Depends(get_session)):
    return catalog.list_episodes(session, _title_or_404(session, title_id))


@router.post("/{title_id}/episodes", response_model=EpisodeOut, status_code=201)
def add_episode(title_id: int, data: EpisodeIn, session: Session = Depends(get_session),
                _user: User = Depends(current_user)):
    title = _title_or_404(session, title_id)
    try:
        return catalog.add_episode(session, title, data.model_dump())
    except catalog.CatalogError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
```

`backend/app/routers/tags.py`:

```python
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.deps import get_session
from app.schemas import TagCount
from app.services import catalog

router = APIRouter(prefix="/api/tags", tags=["tags"])


@router.get("", response_model=list[TagCount])
def list_tags(session: Session = Depends(get_session)):
    return catalog.list_tags(session)
```

`backend/app/routers/watchlist.py`:

```python
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.deps import current_user, get_session
from app.models import Title, User
from app.schemas import WatchlistItem
from app.services import catalog

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


def _title_or_404(session: Session, title_id: int) -> Title:
    title = catalog.get_title(session, title_id)
    if title is None:
        raise HTTPException(status_code=404, detail="title not found")
    return title


@router.get("", response_model=list[WatchlistItem])
def list_watchlist(session: Session = Depends(get_session),
                   user: User = Depends(current_user)):
    return catalog.watchlist(session, user)


@router.put("/{title_id}", status_code=204)
def add_to_watchlist(title_id: int, session: Session = Depends(get_session),
                     user: User = Depends(current_user)):
    catalog.add_to_watchlist(session, user, _title_or_404(session, title_id))
    return Response(status_code=204)


@router.delete("/{title_id}", status_code=204)
def remove_from_watchlist(title_id: int, session: Session = Depends(get_session),
                          user: User = Depends(current_user)):
    catalog.remove_from_watchlist(session, user, _title_or_404(session, title_id))
    return Response(status_code=204)
```

`backend/app/main.py`:

```python
from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.db import make_engine, session_factory
from app.routers import tags, titles, watchlist


def create_app(database_url: str | None = None) -> FastAPI:
    """Build the API. `database_url` defaults to REELOG_DATABASE_URL, then a
    local SQLite file. Tooling depends on this signature."""
    url = database_url or os.environ.get("REELOG_DATABASE_URL", "sqlite:///reelog.db")
    app = FastAPI(title="Reelog")
    app.state.sessions = session_factory(make_engine(url))
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:3000"],
                       allow_methods=["*"], allow_headers=["*"])
    for router in (titles.router, tags.router, watchlist.router):
        app.include_router(router)
    return app
```

`backend/app/seed.py`:

```python
"""Load demo data: `uv run python -m app.seed` (honours REELOG_DATABASE_URL)."""
from __future__ import annotations

import os

from sqlalchemy import func, select

from app.db import make_engine, session_factory
from app.migrate import upgrade
from app.models import User
from app.services import catalog

DEMO_TITLES = [
    {"name": "Arrival", "kind": "film", "year": 2016,
     "synopsis": "A linguist meets visitors.", "tags": ["drama", "sci-fi"]},
    {"name": "Dark", "kind": "series", "year": 2017,
     "synopsis": "Four families, one town, many times.", "tags": ["mystery", "sci-fi"]},
    {"name": "The Grand Budapest Hotel", "kind": "film", "year": 2014,
     "synopsis": "A concierge and his lobby boy.", "tags": ["comedy"]},
    {"name": "Babylon Berlin", "kind": "series", "year": 2017,
     "synopsis": "Berlin, 1929.", "tags": ["crime", "drama"]},
    {"name": "Portrait of a Lady on Fire", "kind": "film", "year": 2019,
     "synopsis": "Brittany, 1770.", "tags": ["drama", "romance"]},
]


def seed(database_url: str) -> None:
    upgrade(database_url)
    sessions = session_factory(make_engine(database_url))
    with sessions() as session:
        if session.scalar(select(func.count(User.id))):
            return
        session.add(User(name="Demo", api_key="demo-key"))
        for data in DEMO_TITLES:
            title = catalog.create_title(session, data)
            if title.kind == "series":
                for number in range(1, 4):
                    catalog.add_episode(session, title, {
                        "season": 1, "number": number, "name": f"Episode {number}"})
        session.commit()


if __name__ == "__main__":
    seed(os.environ.get("REELOG_DATABASE_URL", "sqlite:///reelog.db"))
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd fixtures/reelog/backend && uv run --frozen pytest -q && uv run --frozen ruff check . && uv run --frozen python -m app.seed && rm -f reelog.db`
Expected: `21 passed` (including parametrized cases); `All checks passed!`; seeding exits 0.

- [ ] **Step 6: Commit**

```bash
git add fixtures/reelog/.gitignore fixtures/reelog/backend
git commit -F - <<'EOF'
feat(reelog): FastAPI catalog backend fixture

Titles, episodes, tags and a watchlist on SQLAlchemy 2 + Alembic, with
public pytest coverage and a migration/model drift check. Carries
deliberate debt: a god service, N+1 listing, a floor-division page count.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RbtkGAHGVyLqrM25Tdxw2w
EOF
```

---

### Task 3: Reelog frontend

**Files (all under `fixtures/reelog/frontend/`):**
- Create: `package.json`, `package-lock.json` (generated), `tsconfig.json`, `next.config.mjs`, `next-env.d.ts`, `eslint.config.mjs`, `vitest.config.ts`, `vitest.setup.ts`
- Create: `app/layout.tsx`, `app/globals.css`, `app/page.tsx`, `app/titles/[id]/page.tsx`, `app/watchlist/page.tsx`
- Create: `components/Catalog.tsx`, `components/Pagination.tsx`, `components/SearchBox.tsx`, `components/TagList.tsx`, `components/TitleCard.tsx`, `components/TitleDetail.tsx`, `components/Watchlist.tsx`, `components/WatchlistButton.tsx`
- Create: `lib/api.ts`, `lib/config.ts`, `lib/format.ts`, `lib/types.ts`
- Test: `tests/format.test.ts`, `tests/TitleCard.test.tsx`, `tests/Pagination.test.tsx`, `tests/Catalog.test.tsx`

**Interfaces:**
- Consumes: Task 2's HTTP API and JSON shapes.
- Produces (stable, used by hidden tests): default exports `components/TitleCard` (`{ title: Title }`), `components/Pagination` (`{ page, totalPages, onPage }`), `components/TitleDetail` (`{ titleId: number }`); `lib/api.ts` exports `fetchTitles`, `fetchTitle`, `fetchEpisodes`, `fetchWatchlist`, `addToWatchlist`, `removeFromWatchlist`; `lib/format.ts` exports `formatEpisodeCode`, `pluralize`; `lib/types.ts` exports `Title`, `Page`, `Episode`, `WatchlistItem`; path alias `@/*`.
- Deliberate debt (keep it): every `lib/api.ts` function repeats its own fetch and error handling.

- [ ] **Step 1: Scaffold and install**

`frontend/package.json`:

```json
{
  "name": "reelog-frontend",
  "version": "0.1.0",
  "private": true,
  "scripts": {
    "dev": "next dev",
    "build": "next build",
    "typecheck": "tsc --noEmit",
    "lint": "eslint .",
    "test": "vitest run"
  }
}
```

Run:

```bash
cd fixtures/reelog/frontend
npm install --save-exact next@15 react@19 react-dom@19
npm install --save-exact -D typescript@5 @types/node@22 @types/react@19 @types/react-dom@19 vitest@3 @vitejs/plugin-react@4 jsdom@26 @testing-library/react@16 @testing-library/dom@10 eslint@9 typescript-eslint@8
```

Expected: `package.json` gains exact versions; `package-lock.json` written; `node_modules` created (git-ignored).

`frontend/tsconfig.json`:

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "lib": ["dom", "dom.iterable", "esnext"],
    "skipLibCheck": true,
    "strict": true,
    "noEmit": true,
    "esModuleInterop": true,
    "module": "esnext",
    "moduleResolution": "bundler",
    "resolveJsonModule": true,
    "isolatedModules": true,
    "jsx": "preserve",
    "incremental": true,
    "plugins": [{ "name": "next" }],
    "paths": { "@/*": ["./*"] }
  },
  "include": ["next-env.d.ts", "**/*.ts", "**/*.tsx", ".next/types/**/*.ts"],
  "exclude": ["node_modules"]
}
```

`frontend/next.config.mjs`:

```js
/** @type {import("next").NextConfig} */
const nextConfig = {};

export default nextConfig;
```

`frontend/next-env.d.ts`:

```ts
/// <reference types="next" />
/// <reference types="next/image-types/global" />

// NOTE: This file should not be edited
// see https://nextjs.org/docs/app/api-reference/config/typescript for more information.
```

`frontend/eslint.config.mjs`:

```js
import tseslint from "typescript-eslint";

export default [
  { ignores: [".next/**", "node_modules/**", "next-env.d.ts"] },
  ...tseslint.configs.recommended,
];
```

`frontend/vitest.config.ts`:

```ts
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@": fileURLToPath(new URL("./", import.meta.url)) } },
  test: { environment: "jsdom", setupFiles: ["./vitest.setup.ts"] },
});
```

`frontend/vitest.setup.ts`:

```ts
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => cleanup());
```

- [ ] **Step 2: Write the failing tests**

`frontend/tests/format.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { formatEpisodeCode, pluralize } from "@/lib/format";

describe("formatEpisodeCode", () => {
  it("pads season and episode numbers", () => {
    expect(formatEpisodeCode(1, 2)).toBe("S01E02");
    expect(formatEpisodeCode(12, 104)).toBe("S12E104");
  });
});

describe("pluralize", () => {
  it("uses the singular only for exactly one", () => {
    expect(pluralize(1, "episode")).toBe("1 episode");
    expect(pluralize(0, "episode")).toBe("0 episodes");
    expect(pluralize(3, "person", "people")).toBe("3 people");
  });
});
```

`frontend/tests/TitleCard.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import TitleCard from "@/components/TitleCard";
import type { Title } from "@/lib/types";

const film: Title = {
  id: 1, name: "Arrival", kind: "film", year: 2016, synopsis: "", tags: ["drama", "sci-fi"],
  episode_count: 0,
};

describe("TitleCard", () => {
  it("links to the title and shows its year, kind and tags", () => {
    render(<TitleCard title={film} />);

    expect(screen.getByRole("link", { name: "Arrival" }).getAttribute("href")).toBe("/titles/1");
    expect(screen.getByText("2016")).toBeTruthy();
    expect(screen.getByText("Film")).toBeTruthy();
    expect(screen.getByRole("list", { name: "Tags" }).textContent).toBe("dramasci-fi");
  });

  it("counts episodes only for series", () => {
    render(<TitleCard title={{ ...film, id: 2, name: "Dark", kind: "series", episode_count: 26 }} />);

    expect(screen.getByText("26 episodes")).toBeTruthy();
  });
});
```

`frontend/tests/Pagination.test.tsx`:

```tsx
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import Pagination from "@/components/Pagination";

function button(name: string): HTMLButtonElement {
  return screen.getByRole("button", { name }) as HTMLButtonElement;
}

describe("Pagination", () => {
  it("disables Previous on the first page and Next on the last", () => {
    render(<Pagination page={1} totalPages={1} onPage={() => {}} />);

    expect(button("Previous").disabled).toBe(true);
    expect(button("Next").disabled).toBe(true);
  });

  it("moves to the neighbouring page", () => {
    const onPage = vi.fn();
    render(<Pagination page={2} totalPages={3} onPage={onPage} />);

    fireEvent.click(button("Next"));
    fireEvent.click(button("Previous"));

    expect(onPage.mock.calls).toEqual([[3], [1]]);
    expect(screen.getByText("Page 2 of 3")).toBeTruthy();
  });
});
```

`frontend/tests/Catalog.test.tsx`:

```tsx
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Catalog from "@/components/Catalog";
import { fetchTitles } from "@/lib/api";
import type { Page } from "@/lib/types";

vi.mock("@/lib/api", () => ({ fetchTitles: vi.fn() }));

function page(overrides: Partial<Page> = {}): Page {
  return {
    items: [{ id: 1, name: "Arrival", kind: "film", year: 2016, synopsis: "", tags: [],
              episode_count: 0 }],
    page: 1, page_size: 20, total: 1, total_pages: 1, has_next: false, ...overrides,
  };
}

describe("Catalog", () => {
  beforeEach(() => {
    vi.mocked(fetchTitles).mockReset();
  });

  it("shows the first page of titles", async () => {
    vi.mocked(fetchTitles).mockResolvedValue(page());

    render(<Catalog />);

    expect(await screen.findByRole("link", { name: "Arrival" })).toBeTruthy();
    expect(fetchTitles).toHaveBeenCalledWith({ page: 1, pageSize: 20, q: undefined });
  });

  it("searches from the first page with a trimmed query", async () => {
    vi.mocked(fetchTitles).mockResolvedValue(page({ page: 2, total_pages: 3 }));
    render(<Catalog />);
    await screen.findByText("Page 2 of 3");

    fireEvent.change(screen.getByLabelText("Search titles"), { target: { value: " dune " } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));

    await vi.waitFor(() => expect(fetchTitles).toHaveBeenLastCalledWith(
      { page: 1, pageSize: 20, q: "dune" }));
  });
});
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd fixtures/reelog/frontend && npm test`
Expected: FAIL — `Failed to resolve import "@/lib/format"` (and the components) in all four files.

- [ ] **Step 4: Implement the frontend**

`frontend/lib/types.ts`:

```ts
export type TitleKind = "film" | "series";

export interface Title {
  id: number;
  name: string;
  kind: TitleKind;
  year: number;
  synopsis: string;
  tags: string[];
  episode_count: number;
}

export interface Page {
  items: Title[];
  page: number;
  page_size: number;
  total: number;
  total_pages: number;
  has_next: boolean;
}

export interface Episode {
  id: number;
  season: number;
  number: number;
  name: string;
}

export interface WatchlistItem {
  title: Title;
  added_at: string;
}
```

`frontend/lib/config.ts`:

```ts
export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
export const API_KEY = process.env.NEXT_PUBLIC_API_KEY ?? "demo-key";
```

`frontend/lib/format.ts`:

```ts
export function formatEpisodeCode(season: number, number: number): string {
  return `S${String(season).padStart(2, "0")}E${String(number).padStart(2, "0")}`;
}

export function pluralize(count: number, singular: string, plural = `${singular}s`): string {
  return `${count} ${count === 1 ? singular : plural}`;
}
```

`frontend/lib/api.ts`:

```ts
import { API_URL } from "@/lib/config";
import type { Episode, Page, Title, WatchlistItem } from "@/lib/types";

export async function fetchTitles(
  params: { page?: number; pageSize?: number; q?: string },
): Promise<Page> {
  const search = new URLSearchParams();
  if (params.page) search.set("page", String(params.page));
  if (params.pageSize) search.set("page_size", String(params.pageSize));
  if (params.q) search.set("q", params.q);
  const response = await fetch(`${API_URL}/api/titles?${search}`);
  if (!response.ok) throw new Error(`Could not load titles (${response.status})`);
  return response.json();
}

export async function fetchTitle(id: number): Promise<Title> {
  const response = await fetch(`${API_URL}/api/titles/${id}`);
  if (!response.ok) throw new Error(`Could not load title ${id} (${response.status})`);
  return response.json();
}

export async function fetchEpisodes(id: number): Promise<Episode[]> {
  const response = await fetch(`${API_URL}/api/titles/${id}/episodes`);
  if (!response.ok) throw new Error(`Could not load episodes (${response.status})`);
  return response.json();
}

export async function fetchWatchlist(apiKey: string): Promise<WatchlistItem[]> {
  const response = await fetch(`${API_URL}/api/watchlist`, { headers: { "X-API-Key": apiKey } });
  if (!response.ok) throw new Error(`Could not load the watchlist (${response.status})`);
  return response.json();
}

export async function addToWatchlist(id: number, apiKey: string): Promise<void> {
  const response = await fetch(`${API_URL}/api/watchlist/${id}`, {
    method: "PUT", headers: { "X-API-Key": apiKey },
  });
  if (!response.ok) throw new Error(`Could not add to the watchlist (${response.status})`);
}

export async function removeFromWatchlist(id: number, apiKey: string): Promise<void> {
  const response = await fetch(`${API_URL}/api/watchlist/${id}`, {
    method: "DELETE", headers: { "X-API-Key": apiKey },
  });
  if (!response.ok) throw new Error(`Could not remove from the watchlist (${response.status})`);
}
```

`frontend/components/TagList.tsx`:

```tsx
export default function TagList({ tags }: { tags: string[] }) {
  if (tags.length === 0) return null;
  return (
    <ul className="tags" aria-label="Tags">
      {tags.map((tag) => <li key={tag}>{tag}</li>)}
    </ul>
  );
}
```

`frontend/components/TitleCard.tsx`:

```tsx
import Link from "next/link";
import TagList from "@/components/TagList";
import { pluralize } from "@/lib/format";
import type { Title } from "@/lib/types";

export default function TitleCard({ title }: { title: Title }) {
  return (
    <article className="title-card" aria-labelledby={`title-${title.id}`}>
      <h2 id={`title-${title.id}`}>
        <Link href={`/titles/${title.id}`}>{title.name}</Link>
      </h2>
      <p className="meta">
        <span>{title.year}</span>
        <span className="kind">{title.kind === "series" ? "Series" : "Film"}</span>
        {title.kind === "series" && <span>{pluralize(title.episode_count, "episode")}</span>}
      </p>
      <TagList tags={title.tags} />
    </article>
  );
}
```

`frontend/components/Pagination.tsx`:

```tsx
"use client";

interface PaginationProps {
  page: number;
  totalPages: number;
  onPage: (page: number) => void;
}

export default function Pagination({ page, totalPages, onPage }: PaginationProps) {
  return (
    <nav aria-label="Pagination" className="pagination">
      <button type="button" onClick={() => onPage(page - 1)} disabled={page <= 1}>
        Previous
      </button>
      <span>Page {page} of {totalPages}</span>
      <button type="button" onClick={() => onPage(page + 1)} disabled={page >= totalPages}>
        Next
      </button>
    </nav>
  );
}
```

`frontend/components/SearchBox.tsx`:

```tsx
"use client";

import { type FormEvent, useState } from "react";

export default function SearchBox({ onSearch }: { onSearch: (query: string) => void }) {
  const [query, setQuery] = useState("");

  function submit(event: FormEvent) {
    event.preventDefault();
    onSearch(query.trim());
  }

  return (
    <form role="search" onSubmit={submit}>
      <label htmlFor="catalog-search">Search titles</label>
      <input id="catalog-search" value={query} onChange={(event) => setQuery(event.target.value)} />
      <button type="submit">Search</button>
    </form>
  );
}
```

`frontend/components/Catalog.tsx`:

```tsx
"use client";

import { useEffect, useState } from "react";
import Pagination from "@/components/Pagination";
import SearchBox from "@/components/SearchBox";
import TitleCard from "@/components/TitleCard";
import { fetchTitles } from "@/lib/api";
import type { Page } from "@/lib/types";

export const PAGE_SIZE = 20;

export default function Catalog() {
  const [page, setPage] = useState(1);
  const [query, setQuery] = useState("");
  const [data, setData] = useState<Page | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchTitles({ page, pageSize: PAGE_SIZE, q: query || undefined })
      .then((result) => {
        if (!cancelled) {
          setData(result);
          setError(null);
        }
      })
      .catch((reason: Error) => {
        if (!cancelled) setError(reason.message);
      });
    return () => {
      cancelled = true;
    };
  }, [page, query]);

  function search(next: string) {
    setQuery(next);
    setPage(1);
  }

  return (
    <section>
      <SearchBox onSearch={search} />
      {error && <p role="alert">{error}</p>}
      {data && data.items.length === 0 && <p>No titles match.</p>}
      {data && data.items.map((title) => <TitleCard key={title.id} title={title} />)}
      {data && <Pagination page={data.page} totalPages={data.total_pages} onPage={setPage} />}
    </section>
  );
}
```

`frontend/components/WatchlistButton.tsx`:

```tsx
"use client";

import { useState } from "react";
import { addToWatchlist, removeFromWatchlist } from "@/lib/api";
import { API_KEY } from "@/lib/config";

export default function WatchlistButton(
  { titleId, initiallyOn = false }: { titleId: number; initiallyOn?: boolean },
) {
  const [on, setOn] = useState(initiallyOn);
  const [busy, setBusy] = useState(false);

  async function toggle() {
    setBusy(true);
    try {
      if (on) await removeFromWatchlist(titleId, API_KEY);
      else await addToWatchlist(titleId, API_KEY);
      setOn(!on);
    } finally {
      setBusy(false);
    }
  }

  return (
    <button type="button" onClick={toggle} disabled={busy} aria-pressed={on}>
      {on ? "On watchlist" : "Add to watchlist"}
    </button>
  );
}
```

`frontend/components/TitleDetail.tsx`:

```tsx
"use client";

import { useEffect, useState } from "react";
import TagList from "@/components/TagList";
import WatchlistButton from "@/components/WatchlistButton";
import { fetchEpisodes, fetchTitle } from "@/lib/api";
import { formatEpisodeCode } from "@/lib/format";
import type { Episode, Title } from "@/lib/types";

export default function TitleDetail({ titleId }: { titleId: number }) {
  const [title, setTitle] = useState<Title | null>(null);
  const [episodes, setEpisodes] = useState<Episode[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchTitle(titleId)
      .then(async (loaded) => {
        const list = loaded.kind === "series" ? await fetchEpisodes(titleId) : [];
        if (!cancelled) {
          setTitle(loaded);
          setEpisodes(list);
        }
      })
      .catch((reason: Error) => {
        if (!cancelled) setError(reason.message);
      });
    return () => {
      cancelled = true;
    };
  }, [titleId]);

  if (error) return <p role="alert">{error}</p>;
  if (!title) return <p>Loading…</p>;
  return (
    <article>
      <h1>{title.name}</h1>
      <p className="meta">
        <span>{title.year}</span>
        <span className="kind">{title.kind === "series" ? "Series" : "Film"}</span>
      </p>
      <p>{title.synopsis}</p>
      <TagList tags={title.tags} />
      <WatchlistButton titleId={title.id} />
      {episodes.length > 0 && (
        <ol aria-label="Episodes">
          {episodes.map((episode) => (
            <li key={episode.id}>
              {formatEpisodeCode(episode.season, episode.number)} — {episode.name}
            </li>
          ))}
        </ol>
      )}
    </article>
  );
}
```

`frontend/components/Watchlist.tsx`:

```tsx
"use client";

import { useEffect, useState } from "react";
import TitleCard from "@/components/TitleCard";
import { fetchWatchlist, removeFromWatchlist } from "@/lib/api";
import { API_KEY } from "@/lib/config";
import type { WatchlistItem } from "@/lib/types";

export default function Watchlist() {
  const [items, setItems] = useState<WatchlistItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchWatchlist(API_KEY).then(setItems).catch((reason: Error) => setError(reason.message));
  }, []);

  async function remove(id: number) {
    await removeFromWatchlist(id, API_KEY);
    setItems((current) => (current ?? []).filter((item) => item.title.id !== id));
  }

  if (error) return <p role="alert">{error}</p>;
  if (!items) return <p>Loading…</p>;
  if (items.length === 0) return <p>Your watchlist is empty.</p>;
  return (
    <ul className="watchlist">
      {items.map((item) => (
        <li key={item.title.id}>
          <TitleCard title={item.title} />
          <button type="button" onClick={() => remove(item.title.id)}>Remove</button>
        </li>
      ))}
    </ul>
  );
}
```

`frontend/app/globals.css`:

```css
body { font-family: system-ui, sans-serif; margin: 0 auto; max-width: 48rem; padding: 1rem; }
.title-card { border-bottom: 1px solid #ddd; padding: 0.75rem 0; }
.meta span + span::before { content: " · "; }
.tags { display: flex; gap: 0.5rem; list-style: none; padding: 0; }
.pagination { display: flex; gap: 1rem; align-items: center; margin-top: 1rem; }
```

`frontend/app/layout.tsx`:

```tsx
import Link from "next/link";
import type { ReactNode } from "react";
import "./globals.css";

export const metadata = { title: "Reelog" };

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <header>
          <nav><Link href="/">Catalog</Link> <Link href="/watchlist">Watchlist</Link></nav>
        </header>
        <main>{children}</main>
      </body>
    </html>
  );
}
```

`frontend/app/page.tsx`:

```tsx
import Catalog from "@/components/Catalog";

export default function HomePage() {
  return (
    <>
      <h1>Catalog</h1>
      <Catalog />
    </>
  );
}
```

`frontend/app/titles/[id]/page.tsx`:

```tsx
"use client";

import { useParams } from "next/navigation";
import TitleDetail from "@/components/TitleDetail";

export default function TitlePage() {
  const params = useParams<{ id: string }>();
  return <TitleDetail titleId={Number(params.id)} />;
}
```

`frontend/app/watchlist/page.tsx`:

```tsx
import Watchlist from "@/components/Watchlist";

export default function WatchlistPage() {
  return (
    <>
      <h1>Watchlist</h1>
      <Watchlist />
    </>
  );
}
```

- [ ] **Step 5: Run the tests and quality gates**

Run: `cd fixtures/reelog/frontend && npm test && npm run typecheck && npm run lint && npm run build`
Expected: 8 tests pass; `tsc` exits 0; ESLint exits 0; `next build` compiles (it is not a Régie gate, but it proves the app builds). If `next build` rewrote `tsconfig.json` or `next-env.d.ts`, keep its version and rerun `npm run typecheck`. Then `rm -rf .next tsconfig.tsbuildinfo`.

- [ ] **Step 6: Commit**

```bash
git add fixtures/reelog/frontend
git status --short fixtures/reelog  # must list no node_modules, .next or tsbuildinfo
git commit -F - <<'EOF'
feat(reelog): Next.js catalog frontend fixture

Catalog with search and pagination, title detail with episodes, and a
watchlist, tested with vitest and Testing Library. Carries deliberate
debt: repeated fetch/error code in lib/api.ts.

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RbtkGAHGVyLqrM25Tdxw2w
EOF
```

---

### Task 4: Reelog wiring — Régie config, conventions, and a clean-trial check

**Files (all under `fixtures/reelog/`):**
- Create: `regie.toml`, `AGENTS.md`, `README.md`

**Interfaces:**
- Consumes: Tasks 2–3.
- Produces: the fixture's Régie commands (`setup`, `test`, `lint`, `build`), which smoke, validate, and trials run from the fixture root.

- [ ] **Step 1: Write the configuration and conventions**

`fixtures/reelog/regie.toml`:

```toml
base_branch = "main"
test_globs = ["backend/tests/**", "frontend/tests/**"]
eval_trigger_globs = []

[providers]
enabled = ["codex"]

[workflow]
default_tier = "auto"
direct_execution = true
plan_reviews = true
design_reviews = true
final_review = true
knowledge = false
reflection = false
submit_pr = false
max_parallel_tasks = 2

[commands]
setup = "uv sync --frozen --project backend && npm ci --prefix frontend"
test = "uv run --project backend --frozen pytest -q backend/tests && npm --prefix frontend test"
lint = "uv run --project backend --frozen ruff check backend && npm --prefix frontend run lint"
build = "npm --prefix frontend run typecheck"
```

`fixtures/reelog/AGENTS.md`:

```markdown
# Reelog — engineering conventions

Reelog is a film and series catalog with a personal watchlist.

## Backend (`backend/`)

- FastAPI, SQLAlchemy 2, Alembic, SQLite; Python 3.12 managed with uv.
- Routers in `app/routers` stay thin; queries and business rules live in `app/services`.
- Every schema change ships an Alembic migration in `migrations/versions`;
  `tests/test_migrations.py` fails when models and migrations disagree.
- `app.main.create_app(database_url)` and `app.migrate.upgrade(database_url, revision)`
  are used by tooling: keep their signatures.
- Tests: pytest in `backend/tests`, through the `client`, `auth`, and `make_title` fixtures.

## Frontend (`frontend/`)

- Next.js App Router with TypeScript.
- Components in `components/`; API calls only through `lib/api.ts`; shared types in
  `lib/types.ts`; display helpers in `lib/format.ts`.
- Tests: vitest with Testing Library in `frontend/tests`.

## Commands

See `regie.toml` `[commands]`: `setup`, `test`, `lint`, and `build` (TypeScript check).
```

`fixtures/reelog/README.md`:

```markdown
# Reelog

A small film and series catalog: browse and search titles, see episodes, and keep a
watchlist.

- API: `cd backend && uv run python -m app.seed && uv run uvicorn app.main:create_app --factory`
  (http://localhost:8000, demo API key `demo-key`).
- Web: `cd frontend && npm run dev` (http://localhost:3000).

Conventions for contributors are in `AGENTS.md`.
```

- [ ] **Step 2: Prove the fixture in a clean copy, the way a trial sees it**

Run (system `python3` may be 3.9, which lacks `tomllib`, so the check pins 3.12):

```bash
scratch=$(mktemp -d)
rsync -a --exclude node_modules --exclude .venv --exclude .next --exclude __pycache__ \
  --exclude .pytest_cache --exclude .ruff_cache --exclude '*.tsbuildinfo' --exclude '*.db' \
  fixtures/reelog/ "$scratch/"
(cd "$scratch" && git init -q && git add -A \
  && git -c user.name=t -c user.email=t@t commit -qm base \
  && uv run --no-project --python 3.12 python - <<'EOF'
import subprocess, tomllib
commands = tomllib.load(open("regie.toml", "rb"))["commands"]
for name in ("setup", "test", "lint", "build"):
    result = subprocess.run(["/bin/sh", "-c", commands[name]])
    print(name, "exit", result.returncode)
    assert result.returncode == 0, name
status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True).stdout
print("status:", repr(status))
assert status == "", status
EOF
)
rm -rf "$scratch"
```

Expected: `setup exit 0`, `test exit 0`, `lint exit 0`, `build exit 0`, then `status: ''` (Review Focus 1: installed dependencies and caches stay ignored).

- [ ] **Step 3: Commit**

```bash
git add fixtures/reelog/regie.toml fixtures/reelog/AGENTS.md fixtures/reelog/README.md
git commit -F - <<'EOF'
feat(reelog): Régie configuration and contributor conventions

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RbtkGAHGVyLqrM25Tdxw2w
EOF
```

---

### Task 5: Case `reelog-last-page` (bug fix)

**Files:**
- Create: `cases/reelog/last-page/case.toml`, `brief.md`, `intent.md`, `reference.patch` (generated)
- Create: `cases/reelog/last-page/hidden/backend/bench_hidden/conftest.py`, `cases/reelog/last-page/hidden/backend/bench_hidden/test_last_page.py`
- Modify: `tests/test_cli.py` (catalog and evaluator-rejection tests), `docs/METHODOLOGY.md`, `README.md`

**Interfaces:**
- Consumes: Task 2's stable backend interfaces; Phase 0a's `hidden-tests` evaluator and `validate`.
- Produces: case `reelog-last-page`, `track = "ladder"`.

- [ ] **Step 1: Write the case**

`cases/reelog/last-page/case.toml`:

```toml
id = "reelog-last-page"
fixture = "reelog"
description = "A pagination bug: the last partial catalog page is unreachable."
expected_outcome = "completed"
track = "ladder"

[evaluator]
kind = "hidden-tests"
files = "hidden"
commands = ["cd backend && uv run --frozen --no-sync pytest -q -p no:cacheprovider bench_hidden"]
shared_paths = ["backend/.venv", "frontend/node_modules"]
```

`cases/reelog/last-page/brief.md`:

```markdown
# The last catalog page can't be reached

With 25 titles in the catalog the pager says "Page 2 of 2" and Next is greyed out,
so the last five titles are never shown. It works when the number of titles is an
exact multiple of the page size. Every title should be reachable.
```

`cases/reelog/last-page/intent.md`:

```markdown
# Intent — reelog-last-page

Stakeholder: the catalog editor. Hidden from the agent; answers a Product Owner's questions.

- Every title must be reachable through the pager.
- `total_pages` is the ceiling of `total / page_size`, and at least 1: an empty catalog is
  one empty page. `has_next` is true exactly when a later page exists.
- A page past the end returns no items with the same metadata.
- Nothing else about the API or the UI changes; the pager already follows `total_pages`.

If asked:
- Change the page size? No.
- Change the pager UI? Only if needed; it should not be.
```

`cases/reelog/last-page/hidden/backend/bench_hidden/conftest.py`:

```python
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.migrate import upgrade
from app.models import User


@pytest.fixture
def database_url(tmp_path) -> str:
    url = f"sqlite:///{tmp_path / 'hidden.db'}"
    upgrade(url)
    return url


@pytest.fixture
def client(database_url):
    with TestClient(create_app(database_url)) as test_client:
        yield test_client


@pytest.fixture
def auth(client) -> dict[str, str]:
    with client.app.state.sessions() as session:
        session.add(User(name="Grace", api_key="hidden-key"))
        session.commit()
    return {"X-API-Key": "hidden-key"}


@pytest.fixture
def make_title(client, auth):
    def make(**fields):
        body = {"name": "Untitled", "kind": "film", "year": 2020, "synopsis": "", "tags": [],
                **fields}
        response = client.post("/api/titles", json=body, headers=auth)
        assert response.status_code == 201, response.text
        return response.json()
    return make
```

`cases/reelog/last-page/hidden/backend/bench_hidden/test_last_page.py`:

```python
from __future__ import annotations


def _titles(make_title, count):
    for index in range(count):
        make_title(name=f"Title {index:02d}")


def test_a_partial_last_page_is_reachable(client, make_title):
    _titles(make_title, 25)

    second = client.get("/api/titles", params={"page": 2, "page_size": 10}).json()
    third = client.get("/api/titles", params={"page": 3, "page_size": 10}).json()

    assert (second["total_pages"], second["has_next"]) == (3, True)
    assert [item["name"] for item in third["items"]] == [f"Title {i:02d}" for i in range(20, 25)]
    assert (third["total_pages"], third["has_next"]) == (3, False)


def test_an_exact_multiple_has_no_extra_page(client, make_title):
    _titles(make_title, 20)

    second = client.get("/api/titles", params={"page": 2, "page_size": 10}).json()

    assert (second["total_pages"], second["has_next"]) == (2, False)


def test_an_empty_catalog_is_one_empty_page(client):
    body = client.get("/api/titles", params={"page_size": 10}).json()

    assert (body["items"], body["total_pages"], body["has_next"]) == ([], 1, False)


def test_a_page_past_the_end_is_empty_with_the_same_metadata(client, make_title):
    _titles(make_title, 5)

    body = client.get("/api/titles", params={"page": 4, "page_size": 2}).json()

    assert (body["items"], body["total"], body["total_pages"], body["has_next"]) == (
        [], 5, 3, False)
```

- [ ] **Step 2: Write the reference solution and capture it as a patch**

Edit `fixtures/reelog/backend/app/services/catalog.py`: add `import math` as the first import after `from __future__ import annotations` (followed by a blank line before the `sqlalchemy` imports), and replace `total_pages = max(1, total // page_size)` with:

```python
    total_pages = max(1, math.ceil(total / page_size))
```

Append to `fixtures/reelog/backend/tests/test_titles.py`:

```python
def test_a_partial_last_page_is_reachable(client, make_title):
    for index in range(25):
        make_title(name=f"Title {index:02d}")

    last = client.get("/api/titles", params={"page": 3, "page_size": 10}).json()

    assert (last["total_pages"], last["has_next"], len(last["items"])) == (3, False, 5)
```

Capture and restore the fixture:

```bash
git add -N fixtures/reelog
git diff --relative=fixtures/reelog -- fixtures/reelog > cases/reelog/last-page/reference.patch
git reset -q -- fixtures/reelog
git checkout -- fixtures/reelog
git clean -fdq -- fixtures/reelog
git status --short fixtures/reelog   # must print nothing
```

- [ ] **Step 3: Validate the case**

Run: `uv run regie-bench validate reelog-last-page`
Expected: `PASS reelog-last-page` (hidden tests fail on the fixture, the patch applies, public build/test/lint pass, hidden tests pass).

- [ ] **Step 4: Prove the hidden tests are self-contained** (Review Focus 3)

Run:

```bash
uv run python - <<'EOF'
import shutil, tempfile
from pathlib import Path
from regie_bench.acceptance import run_acceptance
from regie_bench.cli import _run, load_cases, prepare_case
case = load_cases()["reelog-last-page"]
trial = prepare_case(case, Path(tempfile.mkdtemp()), "codex")
repo = trial / "repo"
_run(["/bin/sh", "-c", "uv sync --frozen --project backend"], repo)
_run(["git", "apply", str(case.reference)], repo)
shutil.rmtree(repo / "backend" / "tests")          # an agent may rewrite these
print(run_acceptance(case.evaluator, repo)[0])
EOF
```

Expected: `True`.

- [ ] **Step 5: Update the catalog tests and docs**

In `tests/test_cli.py`, `test_case_catalog_covers_distinct_orchestration_outcomes`: replace `assert len(cases) == 8` with

```python
    assert len([case for case in cases.values() if case.track != "ladder"]) == 8
    assert cases["reelog-last-page"].expected_route is None
```

and restrict `test_completed_case_evaluators_reject_the_unchanged_fixture` to script evaluators (hidden-test cases need installed dependencies; `validate` proves them):

```python
    completed = [
        case for case in load_cases().values()
        if case.expected_outcome == "completed" and case.evaluator.kind == "script"
    ]
```

In `docs/METHODOLOGY.md`, add to the benchmark matrix table:

```markdown
| `reelog-last-page` | FastAPI + Next.js | ladder | — | completed | small bug fix on a realistic codebase |
```

and after the matrix paragraph:

```markdown
The **feature ladder** (`track = "ladder"`) runs on Reelog, a FastAPI + Next.js catalog
with realistic conventions and deliberate debt. Ladder cases prescribe no route: Régie
is judged on the outcome, and each case's difficulty is measured with repeated baseline
trials rather than labelled. Every ladder case ships a `reference.patch` that
`regie-bench validate` proves, and an `intent.md` the stakeholder simulator (Phase 1)
answers from.
```

In `README.md`, extend the fixture paragraph with: "The **Reelog** feature ladder (FastAPI + Next.js) adds cases of rising scope whose difficulty is measured, not labelled."

- [ ] **Step 6: Verify and commit**

Run: `uv run pytest -q && uv run ruff check src tests && uv run regie-bench smoke && uv run regie-bench validate`
Expected: all pass; smoke lists `PASS reelog-last-page`; validate lists `PASS reelog-last-page`.

```bash
git add cases/reelog/last-page tests/test_cli.py docs/METHODOLOGY.md README.md
git commit -F - <<'EOF'
feat(ladder): reelog-last-page bug-fix case

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RbtkGAHGVyLqrM25Tdxw2w
EOF
```

---

### Task 6: Case `reelog-runtime` (end-to-end field)

**Files:**
- Create: `cases/reelog/runtime/case.toml`, `brief.md`, `intent.md`, `reference.patch` (generated)
- Create: `cases/reelog/runtime/hidden/backend/bench_hidden/conftest.py`, `cases/reelog/runtime/hidden/backend/bench_hidden/test_runtime.py`, `cases/reelog/runtime/hidden/frontend/bench_hidden/runtime.test.tsx`
- Modify: `docs/METHODOLOGY.md` (matrix row)

**Interfaces:**
- Consumes: Tasks 2–3 stable interfaces; Alembic revision `"0001"`.
- Produces: case `reelog-runtime`, `track = "ladder"`.

- [ ] **Step 1: Write the case**

`cases/reelog/runtime/case.toml`:

```toml
id = "reelog-runtime"
fixture = "reelog"
description = "An end-to-end field: runtime through migration, API, and UI."
expected_outcome = "completed"
track = "ladder"

[evaluator]
kind = "hidden-tests"
files = "hidden"
commands = [
    "cd backend && uv run --frozen --no-sync pytest -q -p no:cacheprovider bench_hidden",
    "cd frontend && npx --no-install vitest run bench_hidden",
]
shared_paths = ["backend/.venv", "frontend/node_modules"]
```

`cases/reelog/runtime/brief.md`:

```markdown
# Show how long films and episodes run

Viewers keep asking how long something is before they commit to it. When editors add
or edit a title they should be able to record its runtime in minutes, and the catalog
cards and the title page should show it in a friendly way, for example "45 min",
"1 h 32 min", or "2 h". Most existing titles have no runtime yet; for those, show
nothing rather than a zero.

Notes from the tech lead:
- API field: `runtime_minutes`, a whole number from 1 to 600, or null when unknown.
- Put the display formatting in `frontend/lib/format.ts` as `formatRuntime(minutes)`.
```

`cases/reelog/runtime/intent.md`:

```markdown
# Intent — reelog-runtime

Stakeholder: the product manager for viewers. Hidden from the agent.

- `runtime_minutes`: optional whole minutes, 1–600; null when unknown. For a series it
  is the typical episode length; it is shown the same way.
- Existing titles keep all their data after the migration and have no runtime.
- Display: under an hour "45 min"; whole hours "2 h"; otherwise "1 h 32 min". Shown on
  catalog cards and on the title page; nothing is shown when unknown.
- Editors work through the API today; no editing UI is needed.

If asked:
- Series? Same field, typical episode length, same display.
- Where to show it? Catalog cards and the title page only.
- Editing screen? Not needed.
```

`cases/reelog/runtime/hidden/backend/bench_hidden/conftest.py` (every case's hidden tests are self-contained, so each case carries its own copy):

```python
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.migrate import upgrade
from app.models import User


@pytest.fixture
def database_url(tmp_path) -> str:
    url = f"sqlite:///{tmp_path / 'hidden.db'}"
    upgrade(url)
    return url


@pytest.fixture
def client(database_url):
    with TestClient(create_app(database_url)) as test_client:
        yield test_client


@pytest.fixture
def auth(client) -> dict[str, str]:
    with client.app.state.sessions() as session:
        session.add(User(name="Grace", api_key="hidden-key"))
        session.commit()
    return {"X-API-Key": "hidden-key"}


@pytest.fixture
def make_title(client, auth):
    def make(**fields):
        body = {"name": "Untitled", "kind": "film", "year": 2020, "synopsis": "", "tags": [],
                **fields}
        response = client.post("/api/titles", json=body, headers=auth)
        assert response.status_code == 201, response.text
        return response.json()
    return make
```

`cases/reelog/runtime/hidden/backend/bench_hidden/test_runtime.py`:

```python
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.main import create_app
from app.migrate import upgrade


def test_existing_titles_survive_the_migration_without_a_runtime(tmp_path):
    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    upgrade(url, "0001")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO titles (id, name, kind, year, synopsis, created_at) "
            "VALUES (1, 'Arrival', 'film', 2016, 'Linguists.', '2024-01-01 00:00:00')"))
    engine.dispose()

    upgrade(url)

    with TestClient(create_app(url)) as client:
        body = client.get("/api/titles/1").json()
    assert (body["name"], body["synopsis"], body["runtime_minutes"]) == (
        "Arrival", "Linguists.", None)


def test_runtime_is_recorded_updated_and_listed(client, auth, make_title):
    title = make_title(name="Arrival", runtime_minutes=116)
    replacement = {"name": "Arrival", "kind": "film", "year": 2016, "synopsis": "",
                   "tags": [], "runtime_minutes": 118}

    updated = client.put(f"/api/titles/{title['id']}", json=replacement, headers=auth).json()
    listed = client.get("/api/titles").json()["items"][0]

    assert title["runtime_minutes"] == 116
    assert updated["runtime_minutes"] == 118
    assert listed["runtime_minutes"] == 118


def test_runtime_is_optional(make_title):
    assert make_title(name="Dune")["runtime_minutes"] is None


@pytest.mark.parametrize("minutes", [0, 601, -5])
def test_implausible_runtimes_are_rejected(client, auth, minutes):
    body = {"name": "Arrival", "kind": "film", "year": 2016, "runtime_minutes": minutes}

    assert client.post("/api/titles", json=body, headers=auth).status_code == 422
```

`cases/reelog/runtime/hidden/frontend/bench_hidden/runtime.test.tsx`:

```tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import TitleCard from "@/components/TitleCard";
import TitleDetail from "@/components/TitleDetail";
import { formatRuntime } from "@/lib/format";
import type { Title } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  fetchTitle: vi.fn(async () => ({
    id: 7, name: "Arrival", kind: "film", year: 2016, synopsis: "", tags: [],
    episode_count: 0, runtime_minutes: 116,
  })),
  fetchEpisodes: vi.fn(async () => []),
  addToWatchlist: vi.fn(),
  removeFromWatchlist: vi.fn(),
}));

const film = {
  id: 1, name: "Arrival", kind: "film", year: 2016, synopsis: "", tags: [], episode_count: 0,
} as Title;

describe("formatRuntime", () => {
  it.each([[45, "45 min"], [60, "1 h"], [92, "1 h 32 min"], [120, "2 h"], [600, "10 h"]])(
    "formats %i minutes as %s", (minutes, expected) => {
      expect(formatRuntime(minutes)).toBe(expected);
    },
  );
});

describe("runtime display", () => {
  it("shows a known runtime on the card", () => {
    render(<TitleCard title={{ ...film, runtime_minutes: 92 } as Title} />);

    expect(screen.getByText("1 h 32 min")).toBeTruthy();
  });

  it("shows nothing on the card when the runtime is unknown", () => {
    const { container } = render(<TitleCard title={{ ...film, runtime_minutes: null } as Title} />);

    expect(container.textContent).not.toMatch(/\bmin\b|\d+ h\b/);
  });

  it("shows the runtime on the title page", async () => {
    render(<TitleDetail titleId={7} />);

    expect(await screen.findByText("1 h 56 min")).toBeTruthy();
  });
});
```

- [ ] **Step 2: Write the reference solution**

`fixtures/reelog/backend/migrations/versions/0002_title_runtime.py`:

```python
"""title runtime

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("titles", sa.Column("runtime_minutes", sa.Integer, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("titles") as batch:
        batch.drop_column("runtime_minutes")
```

`fixtures/reelog/backend/app/models.py` — in `Title`, after `synopsis`:

```python
    runtime_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
```

`fixtures/reelog/backend/app/schemas.py` — `TitleIn` gains `runtime_minutes: int | None = Field(default=None, ge=1, le=600)`; `TitleOut` gains `runtime_minutes: int | None`.

`fixtures/reelog/backend/app/services/catalog.py` — `serialize_title` adds `"runtime_minutes": title.runtime_minutes,` after `"synopsis"`; `_apply` adds `title.runtime_minutes = data.get("runtime_minutes")`.

`fixtures/reelog/backend/tests/test_titles.py` — change `test_a_title_can_be_created_and_fetched`'s expected dict to include `"runtime_minutes": None`, and append:

```python
def test_runtime_is_optional_and_bounded(client, auth, make_title):
    assert make_title(runtime_minutes=92)["runtime_minutes"] == 92
    body = {"name": "Arrival", "kind": "film", "year": 2016, "runtime_minutes": 0}
    assert client.post("/api/titles", json=body, headers=auth).status_code == 422
```

`fixtures/reelog/frontend/lib/types.ts` — `Title` gains `runtime_minutes?: number | null;`.

`fixtures/reelog/frontend/lib/format.ts` — append:

```ts
export function formatRuntime(minutes: number): string {
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  if (hours === 0) return `${rest} min`;
  return rest === 0 ? `${hours} h` : `${hours} h ${rest} min`;
}
```

`fixtures/reelog/frontend/components/TitleCard.tsx` — import `formatRuntime` alongside `pluralize`, and inside `<p className="meta">` after the kind span:

```tsx
        {title.runtime_minutes ? <span>{formatRuntime(title.runtime_minutes)}</span> : null}
```

`fixtures/reelog/frontend/components/TitleDetail.tsx` — import `formatRuntime` alongside `formatEpisodeCode`, and inside `<p className="meta">` after the kind span, the same line.

`fixtures/reelog/frontend/tests/format.test.ts` — append:

```ts
describe("formatRuntime", () => {
  it("reads naturally", () => {
    expect(formatRuntime(45)).toBe("45 min");
    expect(formatRuntime(92)).toBe("1 h 32 min");
    expect(formatRuntime(120)).toBe("2 h");
  });
});
```

(and add `formatRuntime` to that file's import).

Check, capture, and restore:

```bash
(cd fixtures/reelog/backend && uv run --frozen pytest -q && uv run --frozen ruff check .)
(cd fixtures/reelog/frontend && npm test && npm run typecheck && npm run lint)
git add -N fixtures/reelog
git diff --relative=fixtures/reelog -- fixtures/reelog > cases/reelog/runtime/reference.patch
git reset -q -- fixtures/reelog
git checkout -- fixtures/reelog
git clean -fdq -- fixtures/reelog
git status --short fixtures/reelog   # must print nothing
```

Expected: both suites and gates pass before capture; the fixture is clean after.

- [ ] **Step 3: Validate the case**

Run: `uv run regie-bench validate reelog-runtime`
Expected: `PASS reelog-runtime`.

- [ ] **Step 4: Docs and commit**

Add to the `docs/METHODOLOGY.md` matrix:

```markdown
| `reelog-runtime` | FastAPI + Next.js | ladder | — | completed | end-to-end field: migration, API, UI |
```

Run: `uv run pytest -q && uv run regie-bench smoke && uv run regie-bench validate`
Expected: all pass; both ladder cases `PASS`.

```bash
git add cases/reelog/runtime docs/METHODOLOGY.md
git commit -F - <<'EOF'
feat(ladder): reelog-runtime end-to-end field case

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RbtkGAHGVyLqrM25Tdxw2w
EOF
```

---

### Task 7: Case `reelog-list-queries` (behavior-preserving refactor)

**Files:**
- Create: `cases/reelog/list-queries/case.toml`, `brief.md`, `intent.md`, `reference.patch` (generated)
- Create: `cases/reelog/list-queries/hidden/backend/bench_hidden/conftest.py`, `cases/reelog/list-queries/hidden/backend/bench_hidden/test_list_queries.py`
- Modify: `docs/METHODOLOGY.md` (matrix row)

**Interfaces:**
- Consumes: Task 2's listing endpoint and its N+1 debt.
- Produces: case `reelog-list-queries`, `track = "ladder"`.

- [ ] **Step 1: Write the case**

`cases/reelog/list-queries/case.toml`:

```toml
id = "reelog-list-queries"
fixture = "reelog"
description = "A behavior-preserving refactor: constant queries per catalog page."
expected_outcome = "completed"
track = "ladder"

[evaluator]
kind = "hidden-tests"
files = "hidden"
commands = ["cd backend && uv run --frozen --no-sync pytest -q -p no:cacheprovider bench_hidden"]
shared_paths = ["backend/.venv", "frontend/node_modules"]
```

`cases/reelog/list-queries/brief.md`:

```markdown
# The catalog gets slow as it grows

Once the catalog has a few hundred titles the catalog page takes seconds to load, and it
gets worse the more titles we show per page. What the API returns must not change at
all — same fields, same values, same order — it just has to stop doing extra database
work for every title on the page.

Notes from the tech lead:
- Listing a page (`GET /api/titles`) must run a constant number of SQL queries whatever
  the page size, and never more than 4.
```

`cases/reelog/list-queries/intent.md`:

```markdown
# Intent — reelog-list-queries

Stakeholder: the catalog editor, backed by the tech lead. Hidden from the agent.

- Listing responses are identical: fields, values, and order.
- A listing request runs a constant number of SQL queries regardless of page size, at
  most 4.
- Title detail and every other endpoint behave exactly as before.
- No caching layer, no new dependency.

If asked:
- Caching? No.
- The watchlist too? Not in this change.
- Trim the response? No.
```

`cases/reelog/list-queries/hidden/backend/bench_hidden/conftest.py` (every case's hidden tests are self-contained, so each case carries its own copy):

```python
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.migrate import upgrade
from app.models import User


@pytest.fixture
def database_url(tmp_path) -> str:
    url = f"sqlite:///{tmp_path / 'hidden.db'}"
    upgrade(url)
    return url


@pytest.fixture
def client(database_url):
    with TestClient(create_app(database_url)) as test_client:
        yield test_client


@pytest.fixture
def auth(client) -> dict[str, str]:
    with client.app.state.sessions() as session:
        session.add(User(name="Grace", api_key="hidden-key"))
        session.commit()
    return {"X-API-Key": "hidden-key"}


@pytest.fixture
def make_title(client, auth):
    def make(**fields):
        body = {"name": "Untitled", "kind": "film", "year": 2020, "synopsis": "", "tags": [],
                **fields}
        response = client.post("/api/titles", json=body, headers=auth)
        assert response.status_code == 201, response.text
        return response.json()
    return make
```

`cases/reelog/list-queries/hidden/backend/bench_hidden/test_list_queries.py`:

```python
from __future__ import annotations

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

TITLES = 60


def _kind(index: int) -> str:
    return "series" if index % 3 == 0 else "film"


def _episode_count(index: int) -> int:
    return (index % 4) + 1 if _kind(index) == "series" else 0


@pytest.fixture
def seeded(client, auth, make_title):
    for index in range(TITLES):
        title = make_title(name=f"Title {index:02d}", kind=_kind(index),
                           tags=[f"tag-{index % 5}", "all"])
        for number in range(1, _episode_count(index) + 1):
            response = client.post(f"/api/titles/{title['id']}/episodes",
                                   json={"season": 1, "number": number, "name": f"E{number}"},
                                   headers=auth)
            assert response.status_code == 201, response.text
    return client


def _listing(client, params) -> tuple[int, dict]:
    statements: list[str] = []

    def record(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    # Listen on the Engine class: every engine the app creates is observed.
    event.listen(Engine, "before_cursor_execute", record)
    try:
        response = client.get("/api/titles", params=params)
    finally:
        event.remove(Engine, "before_cursor_execute", record)
    assert response.status_code == 200, response.text
    return len(statements), response.json()


def test_listing_uses_a_constant_small_number_of_queries(seeded):
    small, _ = _listing(seeded, {"page_size": 5})
    large, _ = _listing(seeded, {"page_size": 50})

    assert small == large
    assert large <= 4


def test_listing_returns_exactly_what_it_returned_before(seeded):
    _, body = _listing(seeded, {"page": 2, "page_size": 20})

    expected = [{
        "name": f"Title {index:02d}", "kind": _kind(index), "year": 2020, "synopsis": "",
        "tags": sorted({f"tag-{index % 5}", "all"}), "episode_count": _episode_count(index),
    } for index in range(20, 40)]
    assert [{key: value for key, value in item.items() if key != "id"}
            for item in body["items"]] == expected
    assert (body["total"], body["total_pages"], body["page"], body["has_next"]) == (
        TITLES, 3, 2, True)


def test_title_detail_is_unchanged(seeded):
    listed = seeded.get("/api/titles", params={"page_size": 1}).json()["items"][0]

    assert seeded.get(f"/api/titles/{listed['id']}").json() == listed
```

(The page size of 20 keeps `total_pages` independent of the separate floor-division bug.)

- [ ] **Step 2: Write the reference solution**

`fixtures/reelog/backend/app/services/catalog.py` — import `selectinload` (`from sqlalchemy.orm import Session, selectinload`) and load both relationships with the page:

```python
    titles = session.scalars(
        query.options(selectinload(Title.tags), selectinload(Title.episodes))
        .order_by(Title.name, Title.id).offset((page - 1) * page_size).limit(page_size)
    ).all()
```

`fixtures/reelog/backend/tests/test_titles.py` — append:

```python
def test_listing_does_not_query_per_title(client, make_title):
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    for index in range(12):
        make_title(name=f"Title {index:02d}", tags=["drama"])
    statements = []

    def record(*args):
        statements.append(args[2])

    event.listen(Engine, "before_cursor_execute", record)
    try:
        client.get("/api/titles", params={"page_size": 12})
    finally:
        event.remove(Engine, "before_cursor_execute", record)

    assert len(statements) <= 4
```

Check, capture, and restore:

```bash
(cd fixtures/reelog/backend && uv run --frozen pytest -q && uv run --frozen ruff check .)
git add -N fixtures/reelog
git diff --relative=fixtures/reelog -- fixtures/reelog > cases/reelog/list-queries/reference.patch
git reset -q -- fixtures/reelog
git checkout -- fixtures/reelog
git clean -fdq -- fixtures/reelog
git status --short fixtures/reelog   # must print nothing
```

- [ ] **Step 3: Validate the case**

Run: `uv run regie-bench validate reelog-list-queries`
Expected: `PASS reelog-list-queries`.

- [ ] **Step 4: Docs and commit**

Add to the `docs/METHODOLOGY.md` matrix:

```markdown
| `reelog-list-queries` | FastAPI + Next.js | ladder | — | completed | behavior-preserving performance refactor |
```

Run: `uv run pytest -q && uv run ruff check src tests && uv run regie-bench smoke && uv run regie-bench validate`
Expected: all pass; three ladder cases `PASS` in smoke and validate.

```bash
git add cases/reelog/list-queries docs/METHODOLOGY.md
git commit -F - <<'EOF'
feat(ladder): reelog-list-queries refactor case

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RbtkGAHGVyLqrM25Tdxw2w
EOF
```

---

### Task 8: First empirical difficulty and the 0b-2 decision

Nine real baseline trials (three per case) on the Codex subscription; roughly 30–60 minutes of wall time when the three cases run in parallel.

**Files:** none (results land in the git-ignored `results/`).

- [ ] **Step 1: Run the baseline three times per case, in parallel**

Run each in its own background shell:

```bash
uv run regie-bench run reelog-last-page    --regie-root ../regie --provider codex --baseline --label ladder-0b1 --repeat 3
uv run regie-bench run reelog-runtime      --regie-root ../regie --provider codex --baseline --label ladder-0b1 --repeat 3
uv run regie-bench run reelog-list-queries --regie-root ../regie --provider codex --baseline --label ladder-0b1 --repeat 3
```

Expected: nine `result.json` files with `mode: "baseline"`, schema 2, and identical `fixture_sha256` per case.

- [ ] **Step 2: Report**

Run: `uv run regie-bench difficulty --label ladder-0b1`
Expected: one row per case with a class and no comparability warnings.

- [ ] **Step 3: Decide what 0b-2 needs**

Record the table and the decision in the final report:

- If all three cases **fit** with near-zero revisions, the fixture is too easy at this scope: 0b-2's cases must be larger (multi-increment, cross-layer contracts, ambiguity), and the list-queries case is evidence that single-file refactors do not discriminate.
- If a case is **borderline** or **exceeds**, it is the first calibration point: 0b-2 fills the range around it.
- Any trial that failed for an infrastructure reason (setup, network, provider quota) is rerun, not counted.
