#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
build_dir="$(mktemp -d "${TMPDIR:-/tmp}/hex-combat-events.XXXXXX")"
trap 'rm -rf "$build_dir"' EXIT

if [[ "$#" -ne 7 ]]; then
  echo "usage: $0 ATTACK BLOCKERS BEGIN FIRST_STRIKE STANDARD END REMOVED" >&2
  exit 2
fi

client_shared="$repo_root/HexClient/Assembly-CSharp-firstpass/Game/Shared"
mcs -define:COMBAT_EVENT_ORACLE \
  -out:"$build_dir/combat-events-oracle.exe" \
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
  "$client_shared/AttackDeclaredSessionEventArgs.cs" \
  "$client_shared/BlockersAssignedSessionEventArgs.cs" \
  "$client_shared/CombatPhaseResolvedSessionEventArgs.cs" \
  "$client_shared/BeginCombatResolutionSessionEventArgs.cs" \
  "$client_shared/EndCombatResolutionSessionEventArgs.cs" \
  "$client_shared/CombatRemovedSessionEventArgs.cs" \
  "$repo_root/tests/csharp_oracles/combat_events_oracle.cs"

mono "$build_dir/combat-events-oracle.exe" "$@"
