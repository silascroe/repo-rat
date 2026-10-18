# Architecture

Repo Rat is intentionally small: deterministic polling and persistence do the mechanical work, while an optional language-model call handles the one part that benefits from judgment.

![Repo Rat pipeline](assets/pipeline.svg)

The design rule is simple: **state and delivery should remain reliable even when the clever part fails.** The model is therefore an optional collaborator, not a structural dependency.

## Components

- `repo_rat/config.py` loads and validates JSON configuration and environment-variable names.
- `repo_rat/github.py` fetches releases and optional public starred-repository lists from GitHub using only the Python standard library, with optional token authentication and pagination.
- `repo_rat/state.py` owns SQLite persistence, release lifecycle state, run history, delivery recovery, and the cross-process poll lock.
- `repo_rat/judgment.py` provides an OpenAI-compatible JSON judge plus a deterministic heuristic fallback.
- `repo_rat/delivery.py` posts compact Discord messages or appends structured JSON Lines locally.
- `repo_rat/runner.py` resolves the explicit repository list plus any configured starred source, then coordinates polling, first-run seeding, backfill, judgment, persistence, and delivery.
- `repo_rat/cli.py` exposes one-shot, watch, and backfill modes.

## Release lifecycle

GitHub's numeric release ID is used as the stable identity when available. New releases are inserted once into SQLite and move through a small lifecycle:

1. **pending** — fetched but not yet judged.
2. **seeded** — present during a repository's first successful normal poll, intentionally suppressed to prevent an initial notification flood.
3. **interesting** — approved for reporting; the saved judgment is reused if delivery must be retried.
4. **uninteresting** — processed and intentionally ignored.
5. **failed** — judgment failed and remains eligible for retry.

`--backfill` includes seeded releases; normal polls do not.

When `starred_username` is configured, public starred repositories are added to
the explicit list before polling. A failure to fetch that source is recorded as
an error without silently treating the account as having no stars.

## Delivery semantics

Repo Rat aims to report each release once and protects against duplicate work from overlapping local processes with a SQLite-backed poll lock. Judgments are persisted before delivery, so an interruption after judgment does not require the release to be judged again.

There is one unavoidable edge case: if Discord accepts a webhook and the process crashes before the local delivery marker commits, recovery can post the same report again. Exactly-once delivery across an external webhook cannot be guaranteed without cooperation from the remote endpoint.

## Failure behavior

- GitHub failures are isolated per repository.
- Model failures fall back to deterministic heuristics.
- A failed Discord post leaves the release undelivered; the next poll retries it without judging it again.
- Existing undelivered interesting releases are retried without being re-judged.
- State writes are transactional and survive process restarts.

## Runtime footprint

Python 3.11+ is required. There are no runtime third-party Python dependencies and no external database service. SQLite is part of the standard library.
