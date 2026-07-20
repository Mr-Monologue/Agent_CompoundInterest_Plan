# Phase 0 implementation status

Date: 2026-07-17  
Release: 0.1.0

## Completed

- Python package and dependency lock for Python 3.11–3.12.
- FastAPI `/health` and database-aware `/ready` endpoints.
- SQLite WAL policy and first Alembic operational migration.
- `investor db migrate`, `investor doctor`, and version CLI commands.
- Read-only MCP stdio server with `system_health_get`.
- Hermes `investor` SOUL, MCP configuration template, and guarded Skill source.
- Disabled-by-default script and Agent Cron examples.
- Docker Compose and systemd deployment examples.
- Automated API, CLI, migration, MCP, Cron, and safety-contract tests.

## Validation results

- 9 tests passed.
- Ruff lint and formatting passed.
- Mypy strict mode passed for all source packages.
- Alembic migration passed and remained idempotent on repeated upgrade.
- SQLite `quick_check`, WAL mode, and required Phase 0 tables passed.
- Core binary served both health endpoints successfully.
- MCP stdio initialization and tool discovery returned only `system_health_get`.
- Python wheel and source distribution built successfully.

## Environment gates still open

- The current development container has Python 3.12, so doctor reports `DEGRADED` while preserving
  the Python 3.11 production requirement.
- Docker is unavailable in the current container; Compose and image builds remain to be executed on
  the target host.
- Hermes is unavailable in the current container; Profile, Cron, and Weixin fields remain disabled
  until checked against the pinned target version.
- No financial data adapter is enabled yet. AKShare/Wind canary and function contracts belong to the
  first Phase 2 slice and must pass before any unattended sync job is enabled.

## Next development slice

Start Phase 1 with the first vertical transaction path:

1. Add portfolio, account, instrument, transaction-draft, transaction, holding-snapshot, and audit
   models through a second migration.
2. Implement draft creation, one-time confirmation, idempotent commit, and holding reconstruction.
3. Expose read tools plus `transaction_draft_create` and `transaction_draft_commit`.
4. Cover duplicate Weixin messages, expired tokens, reversals, and insufficient shares with tests.

