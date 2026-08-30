# Hex TCG Private Server

This repository builds an authoritative private server for **HEX: Shards of
Fate**. It allows an original game client to connect to a locally controlled
or privately deployed backend while the server owns the game session,
profiles, card collection, rules, card abilities, AI, and persistence.

The project is not a replacement client and it is not just a proxy around the
original game DLLs. The original client remains valuable as a protocol and
rules reference, but the server makes the decisions that matter: whether a
card can be played, which targets are legal, how effects resolve, where cards
move, who has priority, and what the resulting board state is.

Start with:

- [docs/PRIVATE_SERVER_FEATURES.md](docs/PRIVATE_SERVER_FEATURES.md) for the
  implemented/missing feature checklist.
- [docs/CLIENT_SERVER_PROTOCOL.md](docs/CLIENT_SERVER_PROTOCOL.md) for the
  protocol, card zones, event ordering, and gamedata pipeline.
- [HOWTO.md](HOWTO.md) for operational and reverse-engineering notes.
- [RULES.md](RULES.md) for the server's gameplay decisions.
- [docs/COMMANDS.md](docs/COMMANDS.md) for the supported in-game debug
  commands.
- [docs/FEATURE-FLAGS.md](docs/FEATURE-FLAGS.md) for server-configured client
  profile features such as the developer console and replay UI.
- [abilities/README.md](abilities/README.md) for the gamedata-driven ability
  framework and custom ability extension point.

The smaller package READMEs describe individual extension points:
`domain/`, `services/`, `gamemodes/`, and `campaign_chains/`.

## What “authoritative server” means

In a server-authoritative game, clients request actions and display the
result. They do not decide the result. For example, when a player plays a
troop, the server validates the phase and priority, checks the card's current
zone, pays the cost, resolves abilities and triggers, applies combat/state
rules, moves cards between zones, and publishes the resulting events. Both
players receive a consistent projection of that server state, with hidden
information filtered appropriately.

This also gives the server a useful testing boundary: a test can provide a
board state and a player action, then compare the authoritative player/card
diff and board diff with the expected result. Client rendering and animation
are downstream effects rather than the source of truth.

## Why not put the original C# DLLs behind a thin C# layer?

That approach is useful for studying behavior, but it is a poor long-term
server architecture:

- The networked client uses a projection session; the original authoritative
  offline session is a separate launch mode. There is no network switch that
  turns a connected client into an authoritative server.
- Client DLLs assume Unity objects, local scenes, local input, asset bundles,
  a local profile, and client-only UI state. Running them headlessly couples
  game rules to graphics/runtime details and makes concurrency and recovery
  difficult.
- A thin wrapper would inherit opaque lifecycle and memory ownership, native
  32-bit/Mono constraints, and difficult-to-test global state. A Python server
  can use explicit transactions, SQLite state, deterministic fixtures, and
  focused unit/integration tests.
- The client is not a security boundary. A server that trusts client-side
  legality, costs, targets, or state can be desynchronized or exploited.
- Protocol changes, missing client assets, and platform differences become
  harder to isolate when the server is executing the whole client runtime.
- An independent rules engine can serve multiple clients, AI players, replay
  tools, and headless test cases while still using the original client as a
  compatibility reference.

The repository therefore ports the required protocol and rules behavior into
small server-side components: database-backed state, a turn/priority engine,
generic ability resolution, event serialization, and Python AI heuristics.
The disassembled C# sources and extracted records remain references for
wire formats, enums, timing, and data semantics.

### Database capacity note

SQLite is the default development database. Before running tournaments at
32-player scale (up to 16 simultaneous games), evaluate upgrading the server
to PostgreSQL rather than relying on SQLite. SQLite WAL permits concurrent
reads but still has one database-wide writer, and the game engine frequently
updates persisted `game_cards` state. PostgreSQL should be introduced with a
connection pool and short transactions; changing `HEX_DB_PATH` alone is not a
drop-in migration.

## Current release

This repository is being prepared as version **0.1.0**. It is an early,
compatibility-focused private-server release: the feature checklist records
what has been tested and the remaining parity gaps. See
[CHANGELOG.md](CHANGELOG.md) for the release summary and
[docs/RELEASING.md](docs/RELEASING.md) for the GitHub/GHCR release procedure.

## Architecture at a glance

`hconnect_server.py` serves the TCP HConnect protocol on port 9933.
`proxy.py` serves HTTP compatibility/auth and news endpoints on port 8081.
`db.py` owns the shared SQLite connection and reusable database helpers;
`static.py` owns schema creation and server-owned seeds; `battle_engine.py` owns
turn/priority rules; `game_engine.py` serializes session events; and the
`abilities/` framework resolves gamedata-driven effects.

The original client `Data/gamedata` can be mounted into a fresh deployment
through `HEX_GAMEDATA` (with `GAMEDATA` accepted as an alias). The extractor
populates client-derived tables during initial database creation while
server-owned configuration data is seeded locally. The local `Records/`
snapshot is used when available; Docker creates it from the mounted gamedata
on first startup when needed.

## Running locally

```bash
cd /home/ianutley/Hex
bash restart.sh
```

The server listens on TCP 9933 and the proxy on TCP 8081. The tournament pool
scheduler and replay worker run as background services. For a fresh database,
point the environment variable at the original client file before
starting the server when you want to use a particular client build, for example:

```bash
export HEX_GAMEDATA='/path/to/HEX SHARDS OF FATE/Data/gamedata'
bash restart.sh
```

`restart.sh` regenerates the ignored `generated/starter_decks.json` from the
same gamedata source. A complete `Records/` snapshot can be used instead by
setting `HEX_RECORDS` or leaving it at the default location.

For Docker Desktop on Windows, put the SQLite database in a permanent host
directory so accounts, decks, collections, and sessions survive container
replacement. `$HOME\HexServer` is the recommended location (typically
`C:\Users\<username>\HexServer`). Set
`HEX_DB_PATH` to a path inside the container mount; mounting the directory
also preserves SQLite's `-wal` and `-shm` sidecar files. Mount the client
data separately and read-only:

```powershell
$HexState = Join-Path $HOME 'HexServer'
$ClientData = 'D:\SteamLibrary\steamapps\common\HEX SHARDS OF FATE\Data'
New-Item -ItemType Directory -Force $HexState | Out-Null
docker run --rm `
  -p 9933:9933 -p 8081:8081 `
  -v "${HexState}:/hex/state" `
  -v "${ClientData}:/client-data:ro" `
  -e HEX_DB_PATH='/hex/state/hconnect.db' `
  -e HEX_GAMEDATA='/client-data/gamedata' `
  ghcr.io/<github-owner>/<repository>:latest
```

On the first run, the empty host directory creates and seeds a fresh database;
`HEX_GAMEDATA` supplies client-derived data when configured, and Docker
materializes `Records/` from it when needed. A complete `HEX_RECORDS` mount can
be used instead. Later runs reuse the host database, apply the current
`static.py` schema/seeds in place, and do not overwrite player state. The
container validates the selected data source before startup and runs the
direct test scripts serially after a new database is created. Set
`HEX_RUN_TESTS_ON_BOOT=0` to skip that first-start test pass, or
`HEX_FAIL_ON_TEST_FAILURE=1` to prevent the services from starting when a test
fails. The image does not bundle a database snapshot: if `HEX_DB_PATH` is
omitted, the first run creates `/hex/hconnect.db` from the mounted
`HEX_GAMEDATA` file (or a complete `HEX_RECORDS` mount).

### Publishing the image to GHCR

`.github/workflows/publish-ghcr.yml` publishes the image when `master` or a
release tag is pushed. For the 0.1.0 release, create the Git tag `v0.1.0`;
the workflow publishes the semver image:

```text
ghcr.io/ianutley/hex-server:0.1.0
```

The owner/repository portion is derived from `github.repository`, so the
repository must be published as `IanUtley/hex-server` for that exact image
name. Docker image references are conventionally lowercase; GitHub may show
the owner with its display capitalization.

It also publishes `latest` from the default branch, a `v0.1.0` compatibility
tag for release-tag pushes, and an immutable commit-SHA tag. The workflow
runs for pushes to `master`, `v*` tags, and manual workflow dispatches.
The workflow uses the built-in `GITHUB_TOKEN`; no registry password needs to
be added as a repository secret. After the first successful publish, open the
package's GitHub settings and change its visibility to **Public** so users can
pull it anonymously:

```bash
docker pull ghcr.io/ianutley/hex-server:0.1.0
```

Do not publish a runtime `hconnect.db` containing local accounts or gameplay
state. Use a fresh `$HOME\HexServer` state directory for deployments;
the image itself does not contain a database snapshot.

## Pointing a Windows client at the server

The client reads `config.ini` from its game installation. Make a backup, then
edit the `[SystemSettings]` values so the game server and proxy use the
address reachable from the Windows client. The game server and HTTP proxy
are separate ports:

```ini
[SystemSettings]
GameServerIP = 192.168.1.50:9933
CZEAuthUrl = http://192.168.1.50:8081/auth/hexlogin
CZEPayUrl = http://192.168.1.50:8081/auth/hexpaylinkses
NewsEventsURL = http://192.168.1.50:8081/NewsEvents/NewsEvents-{0}.txt
```

Replace `192.168.1.50` with the server's LAN IP, WSL IP, or public DNS/IP.
Do not use `127.0.0.1` unless the client and server run on the same Windows
machine. In a WSL setup, use the WSL address returned by `hostname -I`, not
the Windows loopback address. For a deployed server, use the public address
or load-balancer address and expose both 9933 and 8081.

Some client builds use `UnityConfig.json` instead of `config.ini`; apply the
same values there. The server does not read this file—the client does. The
game UI receives collection and inventory state through server-pushed profile
messages; no third-party collection-sync HTTP API is provided.

After editing the file, fully restart the client. If login works but the game
cannot join a session, check that TCP 9933 is reachable. If login/auth or
news fails, check HTTP 8081 and the proxy log. The server and client logs are
normally more useful than a repeated client retry; see [HOWTO.md](HOWTO.md)
for the log locations and protocol debugging notes.

### Authentication and two-client testing

The proxy supports non-Steam login through `/auth/hexlogin`. For local PvP
testing, use two distinct non-Steam account records and the same server
address in both clients. This avoids Steam's single-instance and ticket
authentication behavior. Steam configuration remains available when Steam
authentication itself is the behavior under test.

## Development workflow

Keep rules and state server-side. Prefer a deterministic fixture and a focused
test over a client-only manual check:

```bash
python3 -m py_compile commands.py hconnect_server.py abilities/__init__.py
python3 tests/tests_set1_sweep.py
python3 tests/tests_set1_pvp_sweep.py
```

Run the sweeps serially because they use temporary databases and the server's
shared SQLite setup must not be initialized concurrently. Before changing a
card effect, trace its `Records/` template through `card_templates`, ability
metadata, targets, conditions, and the BOM before adding custom Python.

## Status and contribution direction

The checklist intentionally distinguishes tested behavior from full parity.
The most valuable contributions are generic: improve gamedata extraction,
implement a missing effect/condition/target type, add a deterministic board
fixture, and verify the emitted client events. Avoid adding card-name branches
when the behavior can be represented by the card template, ability metadata,
or the runtime card instance.

This is a private, compatibility-focused research and testing server. Use
only game assets and accounts you are entitled to use, and keep deployments
behind appropriate network access controls. Full parity is not the goal of
every subsystem; the feature checklist records the remaining gaps.
