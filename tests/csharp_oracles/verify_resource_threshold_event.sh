#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
build_dir="$(mktemp -d "${TMPDIR:-/tmp}/hex-threshold-reader.XXXXXX")"
trap 'rm -rf "$build_dir"' EXIT

if [[ "$#" -ne 1 ]]; then
  echo "usage: $0 PYTHON_CLASS_35_FIXTURE" >&2
  exit 2
fi

mcs -out:"$build_dir/threshold-event-oracle.exe" \
  "$repo_root/tests/csharp_oracles/serialization_attribute_stubs.cs" \
  "$repo_root/tests/csharp_oracles/session_event_reader_stubs.cs" \
  "$repo_root/HexClient/Assembly-CSharp-firstpass/Game/Shared/Serializer.cs" \
  "$repo_root/HexClient/Assembly-CSharp-firstpass/Game/Shared/TurnPhaseUpdatedSessionEventArgs.cs" \
  "$repo_root/HexClient/Assembly-CSharp-firstpass/Game/Shared/AbilityPushedOnChainSessionEventArgs.cs" \
  "$repo_root/HexClient/Assembly-CSharp-firstpass/Game/Shared/CardCountersChangedSessionEventArgs.cs" \
  "$repo_root/HexClient/Assembly-CSharp-firstpass/Game/Shared/EncounterModDialogSessionEventArgs.cs" \
  "$repo_root/HexClient/Assembly-CSharp-firstpass/Game/Shared/GreenLightSessionEventArgs.cs" \
  "$repo_root/HexClient/Assembly-CSharp-firstpass/Game/Shared/AbilityActivationDataRequiredSessionEventArgs.cs" \
  "$repo_root/HexClient/Assembly-CSharp-firstpass/Game/Shared/CardsRevealedSessionEventArgs.cs" \
  "$repo_root/HexClient/Assembly-CSharp-firstpass/Game/Shared/GameStartedSessionEventArgs.cs" \
  "$repo_root/HexClient/Assembly-CSharp-firstpass/Game/Shared/CostInstanceSessionEventArgs.cs" \
  "$repo_root/HexClient/Assembly-CSharp-firstpass/Game/Shared/TargetInstanceSessionEventArgs.cs" \
  "$repo_root/HexClient/Assembly-CSharp-firstpass/Game/Shared/OptionInstanceSessionEventArgs.cs" \
  "$repo_root/HexClient/Assembly-CSharp-firstpass/Game/Shared/PlayerOptionSessionEventArgs.cs" \
  "$repo_root/HexClient/Assembly-CSharp-firstpass/Game/Shared/PlayerOptionListSessionEventArgs.cs" \
  "$repo_root/HexClient/Assembly-CSharp-firstpass/Game/Shared/PlayerUpdatedSessionEventArgs.cs" \
  "$repo_root/HexClient/Assembly-CSharp-firstpass/Game/Shared/PlayerResourceThresholdChangedSessionEventArgs.cs" \
  "$repo_root/tests/csharp_oracles/resource_threshold_event_oracle.cs"

mono "$build_dir/threshold-event-oracle.exe" "$1"
