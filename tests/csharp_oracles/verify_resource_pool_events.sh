#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
build_dir="$(mktemp -d "${TMPDIR:-/tmp}/hex-resource-pool-reader.XXXXXX")"
trap 'rm -rf "$build_dir"' EXIT

if [[ "$#" -ne 4 ]]; then
  echo "usage: $0 CURRENT_POOL TOTAL_POOL CHARGE SPELL_POINTS" >&2
  exit 2
fi

client_shared="$repo_root/HexClient/Assembly-CSharp-firstpass/Game/Shared"
mcs -out:"$build_dir/resource-pool-events-oracle.exe" \
  "$repo_root/tests/csharp_oracles/serialization_attribute_stubs.cs" \
  "$repo_root/tests/csharp_oracles/session_event_reader_stubs.cs" \
  "$client_shared/Serializer.cs" \
  "$client_shared/TurnPhaseUpdatedSessionEventArgs.cs" \
  "$client_shared/AbilityPushedOnChainSessionEventArgs.cs" \
  "$client_shared/CardCountersChangedSessionEventArgs.cs" \
  "$client_shared/EncounterModDialogSessionEventArgs.cs" \
  "$client_shared/GreenLightSessionEventArgs.cs" \
  "$client_shared/AbilityActivationDataRequiredSessionEventArgs.cs" \
  "$client_shared/CardsRevealedSessionEventArgs.cs" \
  "$client_shared/GameStartedSessionEventArgs.cs" \
  "$client_shared/CostInstanceSessionEventArgs.cs" \
  "$client_shared/TargetInstanceSessionEventArgs.cs" \
  "$client_shared/OptionInstanceSessionEventArgs.cs" \
  "$client_shared/PlayerOptionSessionEventArgs.cs" \
  "$client_shared/PlayerOptionListSessionEventArgs.cs" \
  "$client_shared/PlayerUpdatedSessionEventArgs.cs" \
  "$client_shared/PlayerCurrentResourcePoolChangedSessionEventArgs.cs" \
  "$client_shared/PlayerTotalResourcePoolChangedSessionEventArgs.cs" \
  "$client_shared/ChampionChargePointsChangedSessionEventArgs.cs" \
  "$client_shared/ChampionSpellPointsChangedSessionEventArgs.cs" \
  "$repo_root/tests/csharp_oracles/resource_pool_events_oracle.cs"

mono "$build_dir/resource-pool-events-oracle.exe" "$@"
