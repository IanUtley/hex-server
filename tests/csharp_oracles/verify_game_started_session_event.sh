#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
build_dir="$(mktemp -d "${TMPDIR:-/tmp}/hex-game-started-reader.XXXXXX")"
trap 'rm -rf "$build_dir"' EXIT
fixture="${1:-$repo_root/tests/goldens/game_started_session_event.golden}"

if [[ ! -f "$fixture" ]]; then
  HEX_DB_PATH=:memory: python3 \
    "$repo_root/tests/csharp_oracles/generate_game_started_session_fixture.py" \
    "$fixture"
fi

mcs -out:"$build_dir/game-started-session-reader.exe" \
  "$repo_root/tests/csharp_oracles/serialization_attribute_stubs.cs" \
  "$repo_root/tests/csharp_oracles/session_event_reader_stubs.cs" \
  "$repo_root/hex-private/HexClient/Assembly-CSharp-firstpass/Game/Shared/Serializer.cs" \
  "$repo_root/hex-private/HexClient/Assembly-CSharp-firstpass/Game/Shared/TurnPhaseUpdatedSessionEventArgs.cs" \
  "$repo_root/hex-private/HexClient/Assembly-CSharp-firstpass/Game/Shared/AbilityPushedOnChainSessionEventArgs.cs" \
  "$repo_root/hex-private/HexClient/Assembly-CSharp-firstpass/Game/Shared/CardCountersChangedSessionEventArgs.cs" \
  "$repo_root/hex-private/HexClient/Assembly-CSharp-firstpass/Game/Shared/EncounterModDialogSessionEventArgs.cs" \
  "$repo_root/hex-private/HexClient/Assembly-CSharp-firstpass/Game/Shared/GreenLightSessionEventArgs.cs" \
  "$repo_root/hex-private/HexClient/Assembly-CSharp-firstpass/Game/Shared/NetworkPacketSessionEventArgs.cs" \
  "$repo_root/hex-private/HexClient/Assembly-CSharp-firstpass/Game/Shared/AbilityActivationDataRequiredSessionEventArgs.cs" \
  "$repo_root/hex-private/HexClient/Assembly-CSharp-firstpass/Game/Shared/CardsRevealedSessionEventArgs.cs" \
  "$repo_root/hex-private/HexClient/Assembly-CSharp-firstpass/Game/Shared/GameStartedSessionEventArgs.cs" \
  "$repo_root/hex-private/HexClient/Assembly-CSharp-firstpass/Game/Shared/CostInstanceSessionEventArgs.cs" \
  "$repo_root/hex-private/HexClient/Assembly-CSharp-firstpass/Game/Shared/TargetInstanceSessionEventArgs.cs" \
  "$repo_root/hex-private/HexClient/Assembly-CSharp-firstpass/Game/Shared/OptionInstanceSessionEventArgs.cs" \
  "$repo_root/hex-private/HexClient/Assembly-CSharp-firstpass/Game/Shared/PlayerOptionSessionEventArgs.cs" \
  "$repo_root/hex-private/HexClient/Assembly-CSharp-firstpass/Game/Shared/PlayerOptionListSessionEventArgs.cs" \
  "$repo_root/hex-private/HexClient/Assembly-CSharp-firstpass/Game/Shared/PlayerUpdatedSessionEventArgs.cs" \
  "$repo_root/tests/csharp_oracles/game_started_session_event_oracle.cs"

mono "$build_dir/game-started-session-reader.exe" "$fixture"
