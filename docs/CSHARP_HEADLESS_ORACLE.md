# Unity-hosted C# rules oracle

The experimental adapter runs against the original loaded client rules engine
inside Unity's Mono process. It is a way to capture targeted C# behavior while
the original project cannot be rebuilt from this checkout. It complements the
disassembly and the extracted wire-format oracles; it does not establish
Python/C# parity by itself.

## Build

Build the plugin outside the installed client by pointing the script at the
local BepInEx `core` directory and selecting a temporary output path:

```bash
HEX_BEPINEX_CORE=/path/to/BepInEx/core \
HEX_HEADLESS_OUTPUT=/tmp/HexHeadlessUnity.dll \
bash experimental/build_unity_plugin.sh
```

The script also needs the original client assemblies under
`Hex_Data/Managed`. The resulting DLL is compiled against those assemblies and
BepInEx. To execute it, load the plugin in a client test installation with
BepInEx. The adapter waits for the normal `TemplateManager` load and consumes
commands on Unity's main thread. Building the plugin does not install or run
it. The build script references the installed client's own `mscorlib.dll`,
`System.dll`, and `System.Core.dll`; this avoids compiling calls to reflection
operators that the client's older Mono runtime does not provide.
If template loading exceeds 1,200 Unity updates, the runner logs one warning
and keeps waiting; it does not emit a timeout exception on every frame.

To launch the client from Windows PowerShell, use `Start-Process` with the
client directory as its working directory. Skip the launch when Hex is already
open so the probe does not create a second client:

```powershell
$clientExe = 'D:\SteamLibrary\steamapps\common\HEX SHARDS OF FATE\Hex.exe'
if (-not (Get-Process -Name Hex -ErrorAction SilentlyContinue)) {
    Start-Process -FilePath $clientExe `
        -ArgumentList @('+Standalone', 'true') `
        -WorkingDirectory (Split-Path -Path $clientExe -Parent)
}
```

## Capture one ability

After the plugin has loaded, write a card-template GUID and one of its ability
GUIDs to `Hex_Data/HexHeadlessCommand.txt`:

```bash
printf '%s\n' 'oracle <card-guid> <ability-guid> player 12345 67890' \
  > '/path/to/HEX SHARDS OF FATE/Hex_Data/HexHeadlessCommand.txt'
```

The optional final arguments select the source side (`player` or `opponent`)
and deterministic RNG seeds. Defaults are `player`, `12345`, and `67890`. The
result is written to both Unity's temporary cache and
`Hex_Data/HexHeadlessOracle.json`. Selected runtime captures can be preserved
under [`experimental/captures/`](../experimental/captures/) for later
comparison.

`oracle` starts the selected ability directly. For a non-triggered ability on
an Action or Resource, `oracle_cast` builds the client play transaction and
submits it to the adapter queue. The original authoritative transaction
handler validates it and resolves it; spell casts then pass through
`AuthoritativeSessionBase.CastSpell` from the generated Hand before the chain
is driven synchronously. The record includes the transaction class and
validation result. It rejects triggered abilities because this command does
not inject a matching game event. For example, Runic Growth's conditional
transform can be captured after the C# hand-play transaction with:

```bash
printf '%s\n' 'oracle_cast 0216d408-ef4a-9175-0505-db39e905d4f2 83b38285-7daf-8e17-333a-869e8245b03a player 12345 67890' \
  > '/path/to/HEX SHARDS OF FATE/Hex_Data/HexHeadlessCommand.txt'
```

The JSON input identifies whether resolution was direct or followed a client
play transaction, and the top-level action label records the same distinction.
The cast scenario still uses this adapter's deterministic two-player board.
It exercises transaction validation and resolution through the original
authoritative handler, but skips the live turn-state/action-stack scheduler,
network submission in `SessionClient.SubmitTransaction`, and arbitrary server
snapshots.

The scenario constructs the same generic two-player board fixture used by the
metadata matrix, locates the source card and ability by GUID, asks C# target
templates for candidate targets, and resolves that ability through the
original `CreateAbility`/chain resolver. Fixture construction has no
card-name-specific behavior. The snapshot starts after fixture setup and
before deterministic target selection. The JSON keeps all legal candidates
separate from the selected target map and records each root or nested
selection, including its bounds and selected IDs. Non-random choices take the
first legal targets up to the authored maximum; random target templates use
the original C# RNG. Bounds backed by a target variable need an active effect
instance and are reported as unresolved rather than treating every candidate
as selected.

The spell transaction starts with the built-in Play Card activation, matching
`BattleStatePlayCard`. It includes automatic/indexed templates at the selected
play index and uses the C# engine for resolution. The fixture chooses targets
and options deterministically. It has no live `ClientStateCache`, so it cannot
reproduce the `CanUseAbility` option-list gate that determines which automatic
entries Unity includes in an actual play request.

The JSON schema is `hex-csharp-rules-oracle/v2`. It records:

- Client rules assembly identity, scenario, action, source/ability IDs, phase,
  target-map candidates, and RNG seeds.
- Initial and final player/card state, including ordered collections, pools,
  thresholds, state/context, counters, uses, chain contents, and RNG state.
- One globally ordered trace for broadcast and routed session events,
  tracked/published events, option requests and answers, queued transactions,
  and each recorded RNG draw. Session-event entries contain the public event
  fields plus the exact `ToByteArray()` payload as Base64. If a synthetic event
  cannot produce wire bytes, the entry records the serialization error without
  aborting resolution. The source template GUID and candidate target map are
  snapshotted before resolution so transformations or resolver mutation do not
  rewrite the recorded input.
- The fixture outcome, such as target legality, chain progress, remaining
  prompts/chain items, and any exception classification.

This is a controlled ability-resolution oracle, not yet a complete match
oracle. The command does not restore an arbitrary Python snapshot or drive all
actions through the live session state machine. `oracle_cast` queues a client
play transaction and passes it through the original
`AuthoritativeSessionBase.HandleTransaction`, then resolves the resulting
chain synchronously. The fixture-only handler does not run the live scheduler,
and the transaction is not submitted over `SessionClient` networking. The
adapter also exposes `SubmitAndDrive` for scenarios with a compatible state
machine, but no arbitrary Python-state importer or comparator is connected. A
matching seed alone is not proof of RNG parity; compare the recorded draws and
their order. The synthetic fixture also does not exercise either host's
persistence adapter, a full PvE/PvP lifecycle, or the Unity battle UI.
Triggered abilities require an injected matching event to establish
trigger-discovery parity; a direct ability resolution is not evidence that its
trigger timing is correct.

## Shards of Fate resource path

The card is `00e13fdf-b2c3-4fe7-a064-ce4481b24e8d`, with ability
`0240c6cc-57d4-04f3-6e52-00edb986726c`. Run its original resource transaction
with:

```bash
printf '%s\n' 'oracle_cast 00e13fdf-b2c3-4fe7-a064-ce4481b24e8d 0240c6cc-57d4-04f3-6e52-00edb986726c player 12345 67890' \
  > '/mnt/d/SteamLibrary/steamapps/common/HEX SHARDS OF FATE/Hex_Data/HexHeadlessCommand.txt'
```

The parent ability's target `eb7e48cd-1c85-813f-6635-d43f50cf7809` is the
authored `PlayerTargetTemplate` named `You`, with `m_PlayerFilter=Self`.
`PlayerTargetTemplate.EnumerateLegalTargets` represents that player through
their champion card, so the parent activation data contains the owner's player
ID and champion SessionCardId. That mapping is expected. Its effect
`87147a28-49fe-be18-0193-641836bf5cdd` invokes child ability
`8168d9c0-9b56-2f88-22c0-56cd8b9616b9`; the child target
`b5664968-c53c-ccaa-e49e-c7479d155907` filters for a `Standard` subtype
resource controlled by the player in their Deck. The selected resource is a
child-ability target, not the parent player target.

The first v2 run is preserved at
[`experimental/captures/shards_of_fate_pre_fixture_fix_v2.json`](../experimental/captures/shards_of_fate_pre_fixture_fix_v2.json).
It accepted `PlayResourceTransaction`, added the played card to
`PlayedResources`, and increased the total resource pool by one. It selected
no deck resource and emitted no threshold change because the fixture's
resource filler search excluded every resource template with authored ability
data. The fixture now selects `CardTemplate.IsBasicResource()` and allows that
filler's authored abilities, then seeds both players' decks. The rebuilt
Mono-compatible plugin was loaded by a fresh Unity process. Its
`oracle_cast` accepted the resource transaction, resolved four chain steps,
emptied the chain, and emitted two ordered Blood threshold events: +1 to 6,
then +1 to 7 from an initial threshold of 5. A later audit found that the
double gain was a fixture selection error, not valid one-target oracle
evidence. The nested target template has minimum and maximum counts of one,
but the old headless adapter copied both legal Blood Shards in the synthetic
Deck into a locked target map. The effect therefore ran once per candidate.
The rebuilt adapter retains both cards as candidates, selects only within the
authored maximum, and records the nested target selection in
`input.target_selections`. The v3/v4 captures remain useful for their wire
bytes, but their +2 threshold delta must not be treated as the expected card
result. The corrected adapter compiled to
`/tmp/HexHeadlessUnity_targetcap.dll`; it has not yet been loaded for a fresh
oracle run. The repeat captures
[`shards_of_fate_oracle_v3.json`](../experimental/captures/shards_of_fate_oracle_v3.json)
and
[`shards_of_fate_oracle_v4.json`](../experimental/captures/shards_of_fate_oracle_v4.json)
are identical, including snapshots, ordered events, and wire bytes, but repeat
the same selection error. Run
`20261004174032002` consumed the staged command and is visible in the client
log. That oracle run occurred before the local server was started. The
synthetic fixture still bypasses the live state-machine/network path and Unity
battle UI. The generated Python `--contracts`
fixture now follows the root `ActivateAbility` into this nested child. It
seeds a metadata-matched Standard resource and checks the selected target's
provided threshold flags against the emitted
`PlayerResourceThresholdChangedSessionEventArgs` events and threshold state
delta. This is a separate generated state, not a same-state replay of the C#
capture; importing and comparing identical initial state, target, and ordered
output remains open.
After rebuilding and reinstalling the probe, fresh process PID 100300 consumed
the same command as run `20261004181001606`. Its
`HexHeadlessOracle.json` is byte-identical to v4: the resource transaction was
accepted, four chain steps completed, the chain emptied, and the same two Blood
threshold events raised the value to 6 then 7. The installed DLL hash matched
the build output. A further fresh process (PID 53480) exercised the delayed
template path: it logged one warning after 1,200 updates, then completed
template initialization. Its `oracle_cast`, run `20261004182146513`, was also
byte-identical to v4 and completed after initialization. The Unity client
connected to an isolated local HConnect server with practice/encounter trace
capture enabled, but no gameplay transaction or battle UI flow was run. These
runs establish oracle repeatability and transport startup, not client/server
gameplay acceptance.

## Using a capture for U9

For an open semantic rule, first select a concrete ability GUID and document
the exact fixture state. Capture the C# case, then create or select a Python
scenario with the same effective source, players, collections, resources,
targets, phase, and random choices. Compare the initial snapshots before
comparing final state and ordered events; explicitly map client session IDs to
Python IDs. Preserve C# event order, recipient, and payload when identifying a
mismatch. A capture from the current generic fixture is evidence only for that
fixture.

The next parity improvements are to make scenario state a shared input,
replay transaction and trigger paths in the adapter, normalize C# and Python
records into a common comparator format, and add focused scenario families for
the remaining U9 gaps. Until those steps and the applicable client checks are
complete, keep the related U9/U11 work marked partial.
