# Profile-stream feature flags

The server can enable client features by sending string values in the profile
stream. These are not `ESessionFlags`: they are individual strings carried in
`ProfileStreamEventArgs.Data` over DataType `2210`.

The fixed client reads these values while processing the profile stream and
sets static `PlayerProfile` switches. The server sends the configured values
to every authenticated client during profile initialization.

## Configuration

Set `HEX_PROFILE_FLAGS` to a comma-, space-, or semicolon-separated list:

```bash
HEX_PROFILE_FLAGS='allowcon,allowreplay' bash restart.sh
```

Docker example:

```powershell
$HexState = Join-Path $HOME 'HexServer'
New-Item -ItemType Directory -Force $HexState | Out-Null
docker run --rm `
  -p 9933:9933 -p 8081:8081 `
  -v "${HexState}:/hex/state" `
  -e HEX_DB_PATH=/hex/state/hconnect.db `
  -e HEX_PROFILE_FLAGS='allowcon,allowreplay' `
  ghcr.io/<github-owner>/<repository>:latest
```

When `HEX_PROFILE_FLAGS` is absent, the local server's compatibility default is
`allowcon`. The Docker entrypoint overrides that default with an explicitly
empty value, so both the developer console and replay UI are disabled in the
Docker image unless configured. When the variable is present, it replaces the
default. For example, `allowreplay` alone enables replay but leaves the
developer console disabled. An explicitly empty value disables all
profile-stream feature flags.

Changing the environment requires a server restart and a fresh client login;
the values are sent during profile initialization and are not continuously
polled by the client.

## Known flags

| Flag | Client effect | Server status |
|------|---------------|---------------|
| `allowcon` | Enables the backtick developer console. | Enabled by the local server compatibility default; Docker requires explicit opt-in. The server also rejects `!` commands unless this flag is present. |
| `allowreplay` | Enables the replay entry point in the tournament lobby. | Opt-in profile flag; replay list/download services remain separate work. |
| `showcardversions` | Allows the client to display card-version information. | Pass-through supported; use only for client investigation. |
| `timedplat` | Enables the client's timed-platinum profile behavior. | Pass-through supported; not otherwise implemented as a server feature. |

The client currently recognizes these strings in `PlayerProfile`:
`allowcon`, `allowreplay`, `showcardversions`, and `timedplat`. Unknown values
are passed through but have no effect in this client build.

## Replay-specific notes

`allowreplay` only enables the client-side replay UI hook. A usable replay
browser also needs the server-side services that the UI calls:

- `qreplaylst` through ServiceProfile transaction `80000` to list replays.
- `replayfetch` through ServiceGameSession transaction `160000` to download a
  `.replay` file.

The server records session events for debugging and replay capture. The
background `replay_server.py` worker assembles completed PvP sessions into
`game_replays` rows and `.replay` files; the list and fetch endpoints are now
implemented, while complete client playback validation remains. See
[CLIENT_SERVER_PROTOCOL.md](CLIENT_SERVER_PROTOCOL.md) and [HOWTO.md](../HOWTO.md)
for the replay file and event-recording details.

## Session flags are different

`Game.Shared.ESessionFlags` describe the match context, such as `IsTournament`,
`IsStandardPvP`, `IsImmortalPvP`, `IsPractice`, and `IsDuelingPit`. They are
stored in a replay's header so the client can recreate the appropriate battle
context during playback. No `ESessionFlags` value enables the replay feature.
