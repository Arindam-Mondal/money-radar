# Money Radar — Developer Guide

Commands to run, test and poke at every part of Money Radar, grouped by task. Each section says **where** to run things from and **what you should see**.

> This guide grows with the project: every new feature adds its commands and scenarios here.

**Contents**

1. [Conventions](#1-conventions)
2. [One-time setup](#2-one-time-setup)
3. [Run the stack (Docker Compose)](#3-run-the-stack-docker-compose)
4. [Database](#4-database)
5. [Migrations (Alembic)](#5-migrations-alembic)
6. [Tests and quality checks](#6-tests-and-quality-checks)
7. [Gmail: OAuth and token](#7-gmail-oauth-and-token)
8. [Gmail: preview the search filter](#8-gmail-preview-the-search-filter)
9. [Failure scenarios worth trying](#9-failure-scenarios-worth-trying)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. Conventions

- **Shell**: commands are for Git Bash (Windows), bash or zsh. PowerShell differences are noted where they matter.
- **Directory**: `repo/` means the repository root; `backend/` means `cd backend` first.
- **Python**: always run Python through `uv run …`. It uses the project's Python 3.12 and locked dependencies, whatever `python` on your PATH is.
- **Host vs container**: from your machine, Postgres is `127.0.0.1:5433` and the API is `127.0.0.1:8000`. Inside the Compose network they are `db:5432` and `api:8000`.
- **Use `127.0.0.1`, not `localhost`**, from the host. On Windows `localhost` tries IPv6 first and can stall for seconds.

## 2. One-time setup

From `repo/`:

```bash
cp .env.example .env                  # then set POSTGRES_PASSWORD (letters and digits)
uv tool install pre-commit            # once per machine
pre-commit install                    # once per clone: runs checks on every commit
```

From `backend/`:

```bash
uv sync                               # creates backend/.venv (editor + tests)
```

Point your editor's Python interpreter at `backend/.venv` so imports resolve.

## 3. Run the stack (Docker Compose)

Docker Desktop must be running (`docker info` should succeed). From `repo/`:

| Task | Command | Expect |
|---|---|---|
| Start everything (build, migrate, wait until healthy) | `docker compose up -d --build --wait` | `db` healthy → `migrate` exits 0 → `api` healthy |
| Status | `docker compose ps -a` | `migrate` shows `Exited (0)`; `db` and `api` `healthy` |
| Liveness | `curl -s http://127.0.0.1:8000/health` | `{"status":"ok"}` |
| Readiness (DB reachable) | `curl -s http://127.0.0.1:8000/health/ready` | `{"status":"ok","checks":{"database":"ok"}}` |
| API docs | open http://127.0.0.1:8000/docs | Swagger UI |
| Follow logs | `docker compose logs -f api` | uvicorn access log |
| Migration output | `docker compose logs migrate` | `Running upgrade … -> 0001` (or nothing new) |
| Rebuild after code changes | `docker compose up -d --build --wait` | only changed layers rebuild |
| Stop (keep data) | `docker compose down` | containers removed, volume kept |
| **Wipe the database** | `docker compose down -v` | ⚠️ deletes the `pgdata` volume |

Ports are configurable in `.env` (`API_HOST_PORT`, `DB_HOST_PORT`) if 8000 or 5433 is taken.

## 4. Database

| Task | Command (from `repo/`) |
|---|---|
| psql inside the container | `docker compose exec db psql -U money_radar -d money_radar` |
| One-off query | `docker compose exec db psql -U money_radar -d money_radar -c "select * from sync_state;"` |
| psql from the host | `psql "host=127.0.0.1 port=5433 user=money_radar dbname=money_radar"` |

Useful psql commands: `\dt` (tables), `\d messages` (columns, indexes, constraints), `\dT+` (enum types), `\q` (quit).

## 5. Migrations (Alembic)

Run from `backend/` with the stack up. The `DB_HOST`/`DB_PORT` overrides point Alembic at the host-mapped port.

```bash
export DB_HOST=127.0.0.1 DB_PORT=5433       # PowerShell: $env:DB_HOST="127.0.0.1"; $env:DB_PORT="5433"
```

| Task | Command |
|---|---|
| Where is the database? | `uv run --env-file ../.env alembic current` |
| History | `uv run --env-file ../.env alembic history` |
| New migration from model changes | `uv run --env-file ../.env alembic revision --autogenerate --rev-id 0002 -m "short description"` |
| Apply | `uv run --env-file ../.env alembic upgrade head` |
| Undo the last one | `uv run --env-file ../.env alembic downgrade -1` |
| Models and migrations agree? | `uv run --env-file ../.env alembic check` |

Rules: read every generated migration before committing it (autogenerate can't see renames or enum value changes); commit the model and its migration together; never edit a migration that has been applied anywhere, write a new one.

## 6. Tests and quality checks

From `backend/`:

| Task | Command |
|---|---|
| All unit tests | `uv run pytest` |
| Quiet summary | `uv run pytest -q` |
| One file, verbose | `uv run pytest tests/unit/test_gmail_parse.py -v` |
| Tests matching a name | `uv run pytest -k "history"` |
| Stop at first failure, show locals | `uv run pytest -x -l` |
| Slowest tests | `uv run pytest --durations=5` |
| Lint | `uv run ruff check .` |
| Lint and auto-fix | `uv run ruff check --fix .` |
| Format | `uv run ruff format .` |
| Types (strict) | `uv run mypy .` |
| Everything CI runs on code | `uv run ruff check . && uv run ruff format --check . && uv run mypy . && uv run pytest -q` |

From `repo/`:

| Task | Command |
|---|---|
| All pre-commit hooks on every file | `pre-commit run --all-files` |
| CI runs on GitHub | `gh run list --limit 5` / `gh run view --log-failed` |

Unit tests need no database, network or Gmail: Gmail is replaced by `tests/fakes/mail_source.py` (`FakeMailSource`). One test (`test_refreshed_token_file_is_owner_only`) only runs on Linux/macOS, so it shows as skipped on Windows and runs in CI.

## 7. Gmail: OAuth and token

Needs `secrets/client_secret.json` (Google Cloud desktop OAuth client). The token is written to `secrets/gmail_token.json`. Both are git-ignored; never commit, paste or log them.

From `backend/`:

| Scenario | Command | Expect |
|---|---|---|
| First consent | `uv run python -m app.pipeline.gmail.auth` | browser opens → allow read-only → `OK - Gmail reachable, N labels` |
| Token reuse | run it again | no browser, `OK` straight away |
| Force a new consent | `uv run python -m app.pipeline.gmail.auth --force` | browser opens again |
| Test the refresh path | edit only `"expiry"` in the token file to `"2020-01-01T00:00:00Z"`, then run it | `OK`, no browser; `expiry` is about an hour ahead again |
| Revoked access | remove "Money Radar" at https://myaccount.google.com/permissions, then run it | `Gmail token revoked or expired; re-run consent -> starting consent` |

While the Google Cloud app is in **Testing** status, the refresh token expires after **7 days**: you'll see the "revoked or expired" message and need to consent again.

## 8. Gmail: preview the search filter

`scripts/gmail_preview.py` shows what the ingestion search filter (ING-3) matches in your real mailbox. Read-only. It prints senders and subjects to your terminal only: don't paste the output anywhere.

From `backend/`:

| Scenario | Command |
|---|---|
| Last 7 days, newest 20 matches | `uv run python -m scripts.gmail_preview` |
| Longer window, more rows | `uv run python -m scripts.gmail_preview --days 30 --limit 50` |
| How many hits each term adds | `uv run python -m scripts.gmail_preview --days 30 --per-term` |
| Try other terms | `uv run python -m scripts.gmail_preview --days 30 --terms "Rs,INR,debited,credited,UPI" --per-term` |
| See a symbol-only term refused | `uv run python -m scripts.gmail_preview --terms "Rs,₹"` → `ValueError: … has no letters` |

What to look for: a bank or payment app that emails you but **doesn't** appear is a missed message (false negative); find a word from its subject that would catch it and add it to the region pack's `search_terms`. Extra non-bank matches are fine; classification (P3) filters them later.

On Windows, if printing `₹` or emoji fails with `UnicodeEncodeError`, prefix the command with `PYTHONIOENCODING=utf-8`.

## 9. Failure scenarios worth trying

Each one shows a design decision working. From `repo/`, stack up.

| Scenario | How | Expect |
|---|---|---|
| Database down | `docker compose stop db` | `/health` 200, `/health/ready` 503, `api` turns unhealthy but isn't restarted |
| Database back | `docker compose start db` | `/health/ready` 200 again without restarting `api` (`pool_pre_ping`) |
| Network partition | `docker network disconnect money-radar_default money-radar-db-1` | `/health/ready` 503 within ~15 s instead of hanging (TCP keepalives); reconnect with `docker network connect money-radar_default money-radar-db-1` |
| Bad config | set `POLL_INTERVAL_SECONDS=5` in `.env`, then `docker compose up -d --wait` | `api` fails to start; `docker compose logs api` lists the invalid field |
| Unknown region pack | set `REGION_PACKS=mars` in `.env` | startup fails with `unknown region pack(s) ['mars']` (once the worker loads packs, P1.6) |
| Migrations are idempotent | `docker compose up -d --wait` twice | second `migrate` run applies nothing |

## 10. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Alembic or psql hangs for ~10 s from the host | You used `localhost`; use `127.0.0.1` |
| `port is already allocated` on 5432/5433/8000 | Another Postgres or app owns the port; change `DB_HOST_PORT` / `API_HOST_PORT` in `.env` |
| `python` runs Python 2, or imports fail | Use `uv run python …` from `backend/` |
| Editor shows "could not be resolved" | Select `backend/.venv` as the interpreter |
| `docker: Cannot connect to the Docker daemon` | Start Docker Desktop |
| Commit blocked by pre-commit | Read the hook output; `uv run ruff format .` fixes most of it, then `git add` and commit again |
| `git status` shows a file modified but `git diff` is empty | Stale file timestamp; `git add <file>` clears it |
| Gmail: browser opens on every run | Token can't be used; read the message printed before `-> starting consent` |
| Gmail: `access_denied` / app not available | Your Google account isn't a test user on the OAuth consent screen |
