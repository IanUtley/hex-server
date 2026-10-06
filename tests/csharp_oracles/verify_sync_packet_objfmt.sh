#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
build_dir="$(mktemp -d "${TMPDIR:-/tmp}/hex-sync-packet-objfmt.XXXXXX")"
trap 'rm -rf "$build_dir"' EXIT

if [[ "$#" -lt 4 ]]; then
  echo "usage: $0 CLASS_3 CLASS_48 CLASS_22 OUTPUT_3055" >&2
  echo "   or: $0 ID_CSV ROUTING_UID PAYLOAD... OUTPUT_3055" >&2
  exit 2
fi

sed 's/<>f__mg\$cache/__f__mg_cache/g' \
  "$repo_root/HexClient/Assembly-CSharp-firstpass/Game/Shared/Network/ObjFmt.cs" \
  > "$build_dir/ObjFmt.cs"

mcs -out:"$build_dir/sync-packet-objfmt-oracle.exe" \
  "$repo_root/tests/csharp_oracles/serialization_attribute_stubs.cs" \
  "$build_dir/ObjFmt.cs" \
  "$repo_root/tests/csharp_oracles/sync_packet_objfmt_oracle.cs"

mono "$build_dir/sync-packet-objfmt-oracle.exe" "$@"
