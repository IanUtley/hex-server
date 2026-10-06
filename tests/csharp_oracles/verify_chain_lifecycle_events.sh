#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
build_dir="$(mktemp -d "${TMPDIR:-/tmp}/hex-chain-lifecycle.XXXXXX")"
trap 'rm -rf "$build_dir"' EXIT

if [[ "$#" -ne 3 ]]; then
  echo "usage: $0 TOP_RESOLVED REMOVED_TOP CHAIN_EMPTY" >&2
  exit 2
fi

client_shared="$repo_root/HexClient/Assembly-CSharp-firstpass/Game/Shared"
mcs -define:CHAIN_EVENT_ORACLE \
  -out:"$build_dir/chain-lifecycle-events-oracle.exe" \
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
  "$client_shared/TopOfChainResolvedSessionEventArgs.cs" \
  "$client_shared/RemovedTopOfChainSessionEventArgs.cs" \
  "$client_shared/ChainEmptySessionEventArgs.cs" \
  "$repo_root/tests/csharp_oracles/chain_lifecycle_events_oracle.cs"

mono "$build_dir/chain-lifecycle-events-oracle.exe" "$@"
