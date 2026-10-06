#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
build_dir="$(mktemp -d "${TMPDIR:-/tmp}/hex-phase-priority.XXXXXX")"
trap 'rm -rf "$build_dir"' EXIT

if [[ "$#" -ne 3 ]]; then
  echo "usage: $0 PYTHON_CLASS_3_FIXTURE PYTHON_CLASS_48_FIXTURE PYTHON_CLASS_22_FIXTURE" >&2
  exit 2
fi

client_shared="$repo_root/HexClient/Assembly-CSharp-firstpass/Game/Shared"
mcs -out:"$build_dir/phase-priority-events-oracle.exe" \
  "$repo_root/tests/csharp_oracles/serialization_attribute_stubs.cs" \
  "$repo_root/tests/csharp_oracles/session_event_reader_stubs.cs" \
  "$client_shared/Serializer.cs" \
  "$client_shared/AbilityActivationDataRequiredSessionEventArgs.cs" \
  "$client_shared/CardsRevealedSessionEventArgs.cs" \
  "$client_shared/CardCountersChangedSessionEventArgs.cs" \
  "$client_shared/EncounterModDialogSessionEventArgs.cs" \
  "$client_shared/GameStartedSessionEventArgs.cs" \
  "$client_shared/CostInstanceSessionEventArgs.cs" \
  "$client_shared/TargetInstanceSessionEventArgs.cs" \
  "$client_shared/OptionInstanceSessionEventArgs.cs" \
  "$client_shared/PlayerOptionSessionEventArgs.cs" \
  "$client_shared/PlayerOptionListSessionEventArgs.cs" \
  "$client_shared/PlayerUpdatedSessionEventArgs.cs" \
  "$client_shared/TurnPhaseUpdatedSessionEventArgs.cs" \
  "$client_shared/GreenLightSessionEventArgs.cs" \
  "$client_shared/AbilityPushedOnChainSessionEventArgs.cs" \
  "$repo_root/tests/csharp_oracles/phase_priority_events_oracle.cs"

mono "$build_dir/phase-priority-events-oracle.exe" "$1" "$2" "$3"
