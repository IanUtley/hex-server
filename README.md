# Hex TCG Private Server

This repository builds an authoritative private server for **HEX: Shards of
Fate**. It allows an original game client to connect to a locally controlled
or privately deployed backend while the server owns the game session,
profiles, card collection, rules, card abilities, AI, and persistence.

The server is designed to run separately from the game client and can be hosted
on another machine for multiplayer sessions. A player with the original game
installed only needs to change the client configuration to point at the server.

This project was developed with assistance from AI tools such as Codex and
ChatGPT. Some of the documentation was also generated with AI assistance as a
reference for development and debugging.

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

## What is an “authoritative server”?

In a server-authoritative game, clients request actions and display the
result. They do not decide the result. For example, when a player plays a
troop, the server validates the phase and priority, checks the card's current
zone, pays the cost, resolves abilities and triggers, applies combat/state
rules, moves cards between zones, and publishes the resulting events. Both
players receive a consistent projection of that server state, with hidden
information filtered appropriately.

This also makes automated testing possible: a test can set up a board, perform
an action, and check that the result is correct without opening the game
client. The client's rendering and animation are separate from the server's
game decisions.

## Why Python instead of C# or reusing the original DLLs

The original game client is written in C#, so reusing its DLLs might seem like
the simplest approach. However, those files were designed to run the game
client, not a standalone server. They expect client-specific systems such as
graphics, menus, local sessions, and the original game services.

Python is a better fit for the server because it is easier to read, modify,
test, and deploy. It also makes tasks such as managing player accounts,
collections, campaigns, databases, AI opponents, and multiple connected
players straightforward.

The server still uses the original client as an important reference. We use its
game data and observed behaviour to reproduce card rules and client
compatibility, while keeping the actual game state and decisions on the server.

## Current release

This repository is being prepared as version **0.1.0**. It is an early,
compatibility-focused private-server release: the feature checklist records
what has been tested and the remaining parity gaps. See
[CHANGELOG.md](CHANGELOG.md) for the release summary and
[docs/RELEASING.md](docs/RELEASING.md) for the GitHub/GHCR release procedure.

## Running locally via Docker

You do not need to download this repository to run the server locally. You can
use the published Docker image instead.

Docker runs the Hex server in a self-contained container. This means you do not
need to install Python or configure the server components manually.

Install [Docker Desktop](https://docs.docker.com/desktop/setup/install/) using
the official Docker instructions. Docker Desktop is available for Windows,
macOS, and Linux.

You must have HEX: Shards of Fate installed because the server uses the game's
`Data/gamedata` file to create its card and ability data.

The Docker setup:

  - Runs the game server and web proxy together.
  - Makes the server available on ports 9933 and 8081.
  - Keeps your database and player data in a separate folder so they survive container updates.
  - Allows the server to run on Windows, Linux, or macOS through Docker Desktop.

The commands below are for Windows PowerShell. Linux and macOS users will need
to use equivalent paths and shell commands for their systems.

## Starting the server
Once Docker Desktop is installed, open PowerShell and run the following
commands. Update `$ClientData` if the game is installed somewhere else.

```powershell
$HexState = Join-Path $HOME 'HexServer'
$ClientData = 'C:\Program Files (x86)\Steam\steamapps\common\HEX SHARDS OF FATE\Data'
New-Item -ItemType Directory -Force $HexState | Out-Null
docker run --rm `
  -p 9933:9933 -p 8081:8081 `
  -v "${HexState}:/hex/state" `
  -v "${ClientData}:/client-data:ro" `
  -e HEX_DB_PATH='/hex/state/hconnect.db' `
  -e HEX_GAMEDATA='/client-data/gamedata' `
  ghcr.io/ianutley/hex-server:latest
```

After starting the container, configure the Hex client to connect to the
computer running the server.

## Pointing a Windows client at the server

For a local Docker server on the same Windows PC, use Command Prompt and the
default Steam installation path:

```cmd
set "HEX_GAME=C:\Program Files (x86)\Steam\steamapps\common\HEX SHARDS OF FATE"
copy "%HEX_GAME%\config.ini" "%HEX_GAME%\config.ini.backup"
notepad "%HEX_GAME%\config.ini"
```

Update the existing values under `[SystemSettings]` to:

```ini
[SystemSettings]
GameServerIP = 127.0.0.1:9933
CZEAuthUrl = http://127.0.0.1:8081/auth/hexlogin
CZEPayUrl = http://127.0.0.1:8081/auth/hexpaylinkses
NewsEventsURL = http://127.0.0.1:8081/NewsEvents/NewsEvents-{0}.txt
```

If Windows prevents saving under `Program Files (x86)`, start Command Prompt
as Administrator before running these commands. Fully restart the client after
saving. `127.0.0.1` works because both the client and Docker-published ports
are on the same Windows machine.


### Authentication and two-client testing

The proxy supports non-Steam login through `/auth/hexlogin`. For local PvP
testing, use two distinct non-Steam account records and the same server
address in both clients. This avoids Steam's single-instance and ticket
authentication behavior. Steam configuration remains available when Steam
authentication itself is the behavior under test.

## Architecture at a glance

`hconnect_server.py` serves the TCP HConnect protocol on port 9933.
`proxy.py` serves HTTP compatibility/auth and news endpoints on port 8081.
`db.py` owns the shared SQLite connection and reusable database helpers;
`static.py` owns schema creation and server-owned seeds; `battle_engine.py` owns
turn/priority rules; `game_engine.py` serializes session events; and the
`abilities/` framework resolves gamedata-driven effects.

## Database capacity note

SQLite is the default development database. Before running tournaments at
32-player scale (up to 16 simultaneous games), evaluate upgrading the server
to PostgreSQL rather than relying on SQLite. SQLite WAL permits concurrent
reads but still has one database-wide writer, and the game engine frequently
updates persisted `game_cards` state. PostgreSQL should be introduced with a
connection pool and short transactions; changing `HEX_DB_PATH` alone is not a
drop-in migration.
