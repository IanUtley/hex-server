# Changelog

## 0.1.0 — 2026-08-30

Initial documented release candidate for the Hex TCG private server.

Highlights:

- authoritative profile, collection, store, campaign, session, combat, and
  persistence services over the HConnect protocol;
- gamedata-driven card abilities, conditions, targets, triggers, card state,
  death handling, and focused coverage tests;
- corrected core-set booster/full-set mappings, PVE/PVP random-artifact pool
  selection, generated-land exclusion, and Crayburn Castle race packs;
- campaign encounter deck/personality configuration and AI personality
  fallback support;
- Docker/Fargate startup with persistent SQLite state and optional mounted
  client gamedata, current-schema upgrades for persistent databases, and
  versioned GitHub Actions GHCR publishing;
- direct chest/pack opening now removes the consumed inventory item immediately
  in the client stash as well as persisting the consumption server-side.

This is not a full replacement for the original client or a complete rules
parity implementation. See [docs/PRIVATE_SERVER_FEATURES.md](docs/PRIVATE_SERVER_FEATURES.md)
for the tested surface and known gaps.
