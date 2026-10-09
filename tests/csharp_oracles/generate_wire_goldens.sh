#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
build_dir="$(mktemp -d "${TMPDIR:-/tmp}/hex-objfmt-3029.XXXXXX")"
trap 'rm -rf "$build_dir"' EXIT

# The decompiler emitted C# identifiers containing <>; normalize only those
# cache names so Mono can compile the checked-in client ObjFmt source.
sed 's/<>f__mg\$cache/__f__mg_cache/g' \
  "$repo_root/hex-private/HexClient/Assembly-CSharp-firstpass/Game/Shared/Network/ObjFmt.cs" \
  > "$build_dir/ObjFmt.cs"

python3 "$repo_root/tests/csharp_oracles/generate_player_option_fixture.py" \
  "$build_dir/player_option_list_event.golden"
HEX_DB_PATH=:memory: python3 \
  "$repo_root/tests/csharp_oracles/generate_game_started_session_fixture.py" \
  "$repo_root/tests/goldens/game_started_session_event.golden"

mcs -out:"$build_dir/objfmt-oracle.exe" \
  "$repo_root/tests/csharp_oracles/serialization_attribute_stubs.cs" \
  "$build_dir/ObjFmt.cs" \
  "$repo_root/tests/csharp_oracles/objfmt_wire_oracle.cs"
mono "$build_dir/objfmt-oracle.exe" \
  "$repo_root/tests/goldens/3029_pass_priority_datawrapper.golden" \
  "$repo_root/tests/goldens/game_started_3053_csharp.golden" \
  "$repo_root/tests/goldens/3029_activate_ability_targetmap.golden" \
  "$repo_root/tests/goldens/player_option_list_3055_csharp.golden" \
  "$repo_root/tests/goldens/sync_empty_ack_csharp.golden" \
  "$repo_root/tests/goldens/3029_set_turn_phases_datawrapper.golden" \
  "$repo_root/tests/goldens/3029_choose_play_datawrapper.golden" \
  "$repo_root/tests/goldens/3029_choose_draw_datawrapper.golden" \
  "$repo_root/tests/goldens/3029_mulligan_datawrapper.golden" \
  "$repo_root/tests/goldens/3029_accept_starting_hand_datawrapper.golden" \
  "$repo_root/tests/goldens/3029_play_resource_datawrapper.golden" \
  "$repo_root/tests/goldens/3029_play_troop_datawrapper.golden" \
  "$build_dir/player_option_list_event.golden" \
  "$repo_root/tests/goldens/3029_discard_datawrapper.golden" \
  "$repo_root/tests/goldens/3029_play_artifact_datawrapper.golden" \
  "$repo_root/tests/goldens/3029_play_spell_datawrapper.golden" \
  "$repo_root/tests/goldens/3029_play_champion_datawrapper.golden" \
  "$repo_root/tests/goldens/3029_set_ability_activation_data_datawrapper.golden" \
  "$repo_root/tests/goldens/3029_activate_triggered_abilities_datawrapper.golden" \
  "$repo_root/tests/goldens/3029_commit_attack_datawrapper.golden" \
  "$repo_root/tests/goldens/3029_commit_defense_datawrapper.golden" \
  "$repo_root/tests/goldens/3029_assign_damage_datawrapper.golden" \
  "$repo_root/tests/goldens/3029_set_auto_pass_datawrapper.golden" \
  "$repo_root/tests/goldens/3029_cancel_auto_pass_datawrapper.golden" \
  "$repo_root/tests/goldens/3029_priority_sync_datawrapper.golden" \
  "$repo_root/tests/goldens/3029_quit_game_datawrapper.golden" \
  "$repo_root/tests/goldens/3029_encounter_mod_dialog_datawrapper.golden" \
  "$repo_root/tests/goldens/3029_ready_card_datawrapper.golden" \
  "$repo_root/tests/goldens/3029_request_player_options_datawrapper.golden" \
  "$repo_root/tests/goldens/3029_tip_window_closed_datawrapper.golden" \
  "$repo_root/tests/goldens/3029_state_checksum_datawrapper.golden" \
  "$repo_root/tests/goldens/3029_debug_cheat_datawrapper.golden"

python3 "$repo_root/tests/csharp_oracles/generate_player_option_fixture.py" \
  "$repo_root/tests/goldens/player_option_list_event.golden"
bash "$repo_root/tests/csharp_oracles/verify_player_option_list.sh" \
  "$repo_root/tests/goldens/player_option_list_event.golden"
bash "$repo_root/tests/csharp_oracles/verify_game_started_session_event.sh" \
  "$repo_root/tests/goldens/game_started_session_event.golden"
bash "$repo_root/tests/csharp_oracles/verify_reveal_picker_combine.sh"
