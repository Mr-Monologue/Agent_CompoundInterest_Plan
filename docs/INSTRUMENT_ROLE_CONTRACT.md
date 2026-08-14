# Instrument role contract

Version: v0.31.2
Database revision: `0030_instrument_role_contract`

## Audit result

Before v0.31.2, `instrument_list.role` came from `instruments.role`, which stored the classification
chosen when an instrument was registered. The API and MCP operation named
`instrument_role_update`, however, read and immediately changed
`strategy_instrument_configs.role`, the portfolio-local strategy role. The shared name hid two
different meanings and the immediate update bypassed the strategy configuration draft and
confirmation boundary.

`strategy_current_get` already used the portfolio strategy configuration. Weekly plans also used
that configuration and copied the selected role into immutable plan items. Holdings preferred the
strategy role but previously fell back to registration metadata when no strategy configuration
existed; portfolio brief then consumed the holding role. Repository clients using the ambiguous
field were limited to the CLI/raw API presentation, the Hermes skill instructions and contract
tests.

## Canonical contract

- `registration_role`: classification saved with the global instrument registration record.
- `strategy_role`: role in the active portfolio strategy. This is authoritative for holdings,
  workspace views, reports and weekly plans. A missing strategy configuration is `UNASSIGNED`.
- `role`: deprecated compatibility alias. Instrument-list responses alias `registration_role`;
  strategy, holding and plan responses alias `strategy_role`. Every such response includes a
  deprecation explanation.

An instrument list without a resolved portfolio remains available for older setup clients. In
that case `strategy_role` is `null`; once a portfolio context is supplied or resolved, it reports
the active strategy role.

The database migration renames `instruments.role` to `instruments.registration_role` without
changing existing values.

## Mutation governance

`strategy_instrument_role_draft_create` validates the caller's last observed strategy role and
creates a normal strategy configuration draft while preserving all other active configuration.
It does not change the active strategy. The existing draft commit operation and explicit user
confirmation are still required.

The old `instrument_role_update` API and MCP tool remain available for compatible clients, are
marked deprecated, and now create the same governed draft. They no longer perform an immediate
mutation. No public endpoint changes registration metadata in this release.

## Deployment handoff

Hermes owns Windows deployment and production acceptance. Before deployment, back up the configured
production database using the established installer workflow. After deployment, verify `/ready`,
version `0.31.2`, revision `0030_instrument_role_contract`, tool discovery and read-only role output.
Confirm that known mismatches such as 040046, 000083 and 003765 show both canonical roles without
changing either value. Do not create or commit strategy drafts as part of read-only acceptance.
