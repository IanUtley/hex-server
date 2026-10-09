#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
build_dir="$(mktemp -d "${TMPDIR:-/tmp}/hex-card-zone-events.XXXXXX")"
trap 'rm -rf "$build_dir"' EXIT

if [[ "$#" -ne 12 ]]; then
  echo "usage: $0 PYTHON_CLASS_7_8_10_11_12_14_15_16_17_18_19_50_FIXTURES..." >&2
  exit 2
fi

client_shared="$repo_root/hex-private/HexClient/Assembly-CSharp-firstpass/Game/Shared"
mcs -out:"$build_dir/card-zone-events-oracle.exe" \
  "$repo_root/tests/csharp_oracles/serialization_attribute_stubs.cs" \
  "$repo_root/tests/csharp_oracles/session_event_reader_stubs.cs" \
  "$client_shared/Serializer.cs" \
  "$client_shared/AbilityActivationDataRequiredSessionEventArgs.cs" \
  "$client_shared/CardsRevealedSessionEventArgs.cs" \
  "$client_shared/GameStartedSessionEventArgs.cs" \
  "$client_shared/CostInstanceSessionEventArgs.cs" \
  "$client_shared/TargetInstanceSessionEventArgs.cs" \
  "$client_shared/OptionInstanceSessionEventArgs.cs" \
  "$client_shared/PlayerOptionSessionEventArgs.cs" \
  "$client_shared/PlayerOptionListSessionEventArgs.cs" \
  "$client_shared/PlayerUpdatedSessionEventArgs.cs" \
  "$client_shared/TurnPhaseUpdatedSessionEventArgs.cs" \
  "$client_shared/AbilityPushedOnChainSessionEventArgs.cs" \
  "$client_shared/CardCountersChangedSessionEventArgs.cs" \
  "$client_shared/EncounterModDialogSessionEventArgs.cs" \
  "$client_shared/GreenLightSessionEventArgs.cs" \
  "$client_shared/CardDrawnSessionEventArgs.cs" \
  "$client_shared/CardDestroyedSessionEventArgs.cs" \
  "$client_shared/CardDiscardedSessionEventArgs.cs" \
  "$client_shared/CardVoidedSessionEventArgs.cs" \
  "$client_shared/CardGraveyardedSessionEventArgs.cs" \
  "$client_shared/ChampionCardPlayedSessionEventArgs.cs" \
  "$client_shared/TroopCardPlayedSessionEventArgs.cs" \
  "$client_shared/ResourceCardPlayedSessionEventArgs.cs" \
  "$client_shared/SpellCardPlayedSessionEventArgs.cs" \
  "$client_shared/SpellCardCastSessionEventArgs.cs" \
  "$client_shared/ArtifactCardPlayedSessionEventArgs.cs" \
  "$client_shared/CardMovedSessionEventArgs.cs" \
  "$repo_root/tests/csharp_oracles/card_zone_events_oracle.cs"

mono "$build_dir/card-zone-events-oracle.exe" "$@"
