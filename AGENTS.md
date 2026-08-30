# Hex TCG Private Server — AI Assistant Instructions

Always read `/home/ianutley/Hex/HOWTO.md` before making changes to this project.
HOWTO.md contains architecture docs, encoding rules, card zone events, database
schema details, and client data format information.

**CRITICAL: Never run `git checkout` or `git revert` without first committing or
stashing all changes.**  Doing so will silently discard all in-progress work
across the entire file, requiring careful re-application of every edit.  Always
`git stash` or `git commit` first.

Key files:
- `hconnect_server.py` — Main HConnect protocol server (port 9933)
- `proxy.py` — HTTP proxy for Steam auth (port 8081)
- `commands.py` — In-game chat/debug commands (`!` prefix)
- `campaign.py` — Campaign/adventure service handler (dt=110000)
- `game_engine.py` — Game session event system (60+ SessionEventArgs)
- `encoder.py` — ObjFmt binary encoder (19 encode functions)
- `battle_engine.py` — DB-backed turn/priority engine (phase cycle, stops)
- `game_session.py` — Session state management
- `static.py` — ALL DB DDL + seeded tables (fresh DBs; single source of truth)
- `db.py` — Shared SQLite connection (`db._db`) + reusable `db_*` DML helpers
- `ability.py` - Card ability implementation (BOM leaf executors)
- `AssetExtraction/extract_talents.py` — regenerates talent/ability-BOM seed snapshot
- `migration.py` — run-and-delete one-off migrations (restart.sh)
- `hconnect.db` — SQLite database
- `RULES.md` — canonical gameplay rule set (read before changing battle logic)
- `HexClient` - dissassembly of the Hex client
- `Records` - An extraction of the gamedata blob zip file.

Convention: all schema DDL lives in `static.py` (`static.ensure_schema`); the
shared connection and reusable SQL helpers live in `db.py`; no other module
creates tables. Champion talent/ability seed data is generated from gamedata
by `AssetExtraction/extract_talents.py`, which materializes the seed lists
directly into `static.py` (between `### BEGIN TALENT SEED` / `### END TALENT
SEED`) — rerun the extractor after any gamedata change (never hand-edit that
block).

Note, try not to hard code logic for individual cards in the engine, but rather be data driven from the game_card table, and any and all options and event arguments be based upon the amility/targetting and other fields from within the gamedata for the card and stored in card_template, or against a dynamic version of the card currently in game_card. ONLY USE card_text AS A LAST RESORT.

When fixing a card in either PVP or PVE mode, consider how it should work in the alternate mode, and if a choice is involved, ask the user how it should work in the other mode, then implement the fix for both PVP and PVE.

Use `bash restart.sh` to restart the server after changes to
`hconnect_server.py`. If changes only touched reloadable modules (e.g.
`commands.py`, `domain/*.py`, `ability.py`, `battle_engine.py`), send `SIGUSR1`
to the HConnect process instead; it hot-reloads the runtime modules without a
full restart:

```bash
kill -USR1 "$(pgrep -f '[h]connect_server.py' | head -n1)"
```

## Debugging: check the client log FIRST
The client is a fixed 32 BIT C# Mono binary — so default sizes of ints will be 32bit.
We cannot amend client code. All fixes must be
server-side. The disassembled client C# source is available under `HexClient/`
for reference when investigating client-side behaviour (event handlers, enums,
UI states, animation triggers). When anything fails (stuck UI, missing events, broken flow),
check the client log first before changing server code:

Client logs is at /mnt/d/SteamLibrary/steamapps/common/HEX SHARDS OF FATE/Hex_Data/output_log.txt

Look for `Error`, `Exception`, `Command handler not found`, `NullReferenceException`,
`KeyNotFoundException`, and the client's own `Debug: UIBattle|...|Pushing/Popping UI state`
lines (these show exactly which UI state the client reached and whether it finished). The
client rejects any response it has no handler for (e.g. `Command handler not found for
data wrapper type 3029`) — for fire-and-forget requests (mulligan keep/redraw,
SetTurnPhases) the server must NOT send a reply.

**`KeyNotFoundException` at `Dictionary<SessionCardId,CardRepresentation>.get_Item`** in
`UIBattle.OnTurnPhaseUpdated` → the champion's `ChampionSessionCardId` (from
`PlayerUpdated.ChampionId`) is invalid (UID type 0/undefined). The client's card cache
(`State.Cards`) is corrupted. Every `push_player_updated` must pass a valid champion
SessionCardId. Fix: store champion IDs on handler during init and pass them explicitly.

**`Attempting to create a new SessionCardId with a UID of an undefined type`** → a UID
with a type the client doesn't recognize was sent. Check all SessionCardId-typed fields
in events (CardUpdated, CardMoved, PlayerOptionList, ChampionId in PlayerUpdated).

NOTE: the client sends only ONE transaction at a time (`SessionClient.cs:45`).
If a handled 3029 transaction produced no 3055 sync packet, the NEXT transaction
(incl. Withdraw/`QuitGameTransaction`) is silently dropped client-side. Push an
empty 3055 ack (`_push_transaction_ack`) in that case (see HOWTO.md).
