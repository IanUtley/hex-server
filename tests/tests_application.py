"""Focused tests for application transaction boundaries."""

import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_db import fresh_database

fresh_database()   # bind this process's database before ``db`` is imported

from application import ApplicationCommandDispatcher
from application.commands import (ClaimMailCommand, DeleteMailCommand,
                                  JoinSessionCommand, RemoveSessionCommand,
                                  ServiceRequestCommand,
                                  MarkMailReadCommand,
                                  SetSessionStateCommand,
                                  StartEncounterCommand, StartSessionCommand)
from application.results import SessionRemoved
from application.player_transactions import (classify_player_transaction,
                                              extract_ability_guid,
                                              extract_resource_guid,
                                              typed_payload_from_decoded)
import db


def test_conversation_transaction_is_classified_and_guid_is_extractable():
    raw = (b"EncounterModDialogTransaction;ConversationId;m_Guid;"
           b"11111111-2222-3333-4444-555555555555")
    command = classify_player_transaction(raw)
    assert command.is_encounter_mod_dialog is True
    assert extract_resource_guid(raw, "ConversationId") == \
        "11111111-2222-3333-4444-555555555555"


def test_ability_guid_is_extracted_without_objfmt_field_counting():
    raw = (b"m_AbilityActivationData;AbilityTemplateId;"
           b"m_Guid;95474d1e-ac9b-6c02-cb95-0305ebec42dc;"
           b"SourceCardId;other-guid")
    assert extract_ability_guid(raw) == \
        "95474d1e-ac9b-6c02-cb95-0305ebec42dc"


def test_typed_payload_recovers_objfmt_nested_champion_activation():
    """The live Mono encoding has four ResourceId metadata fields."""
    from types import SimpleNamespace
    raw = (b"m_AbilityActivationData;4;4;0;7;SourceCardId;5;5;1;value;"
           b"6;1;1;m_UID64;7;2;0;0101000000000000;"
           b"AbilityTemplateId;8;6;1;m_Guid;9;7;0;36;"
           b"2f6e6655df575028ba4201067242f4be;"
           b"AbilityInstanceId;ActivateAbilityTransaction")
    command = SimpleNamespace(is_ability_activate=True)
    payload = typed_payload_from_decoded(command, {"__raw__": raw})
    assert payload["source_card_id"] == 257
    assert payload["ability_template_id"] == \
        "2f6e6655-df57-5028-ba42-01067242f4be"
    assert payload["activation_data"] == {}


def test_player_transaction_classifier_recognizes_untyped_manual_activation():
    """Mono can omit the concrete class after a complete activation envelope."""
    command = classify_player_transaction(
        b"m_AbilityActivationData;SourceCardId;m_UID64;0101000000000000;"
        b"AbilityTemplateId;m_Guid;7cc301b8-568a-05f2-d7c2-5414f9e9d6ad;")
    assert command.is_ability_activate is True
    assert command.is_activate_triggered_abilities is False


def test_typed_payload_extractor_reads_only_named_decoded_fields():
    payload = typed_payload_from_decoded(None, {
        "SourceCardId": {"UID": 77},
        "AbilityTemplateId": {"Guid": "ability-guid"},
        "AbilityActivationData": {"target": 3},
        "AbilityInstanceId": 5,
        "UnrelatedUid": 999,
    })
    assert payload == {"card_id": 77, "source_card_id": 77,
                       "ability_template_id": "ability-guid",
                       "activation_data": {"target": 3},
                       "ability_instance_id": 5}


def test_typed_payload_normalizes_csharp_activation_member_names():
    payload = typed_payload_from_decoded(None, {
        "m_AbilityActivationData": [{
            "AbilityInstanceId": 4,
            "OptedIn": True,
            "TargetMap": {"0": [77]},
            "xCostData": {"XCost": 2},
        }],
    })
    assert payload["activation_data"] == ({
        "ability_instance_id": 4,
        "opted": True,
        "target_map": {"0": [77]},
        "x_cost_data": {"x_cost": 2},
        "x_cost": 2,
    },)


def test_typed_activation_target_instances_become_port_target_lists():
    payload = typed_payload_from_decoded(None, {
        "m_AbilityActivationData": [{
            "TargetMap": {"0": {
                "m_SessionCardIds": [{"m_UID64": 0x101}],
                "m_PlayerIds": [],
            }},
        }],
    })
    assert payload["activation_data"][0]["target_map"] == {0: [0x101]}
    player_payload = typed_payload_from_decoded(None, {
        "m_AbilityActivationData": [{
            "TargetMap": {"0": {
                "m_SessionCardIds": [],
                "m_PlayerIds": [{"m_UID64": 0xF401}],
            }},
        }],
    })
    assert player_payload["activation_data"][0]["target_map"] == {0: [0xF401]}


def test_typed_activation_keeps_raw_sacrifice_when_target_map_decodes_first():
    """A separate XCostData sacrifice must survive TargetMap decoding.

    The generic ObjFmt walker can expose the effect target and stop before
    ``CardsToSacrifice``.  Raw recovery still sees both labelled card lists;
    merging those views must leave the cost target distinct from the +2/+2
    target.
    """
    from types import SimpleNamespace

    raw = (
        b"AbilityActivationData;SourceCardId;m_UID64;0;0;0;0101000000000000;"
        b"AbilityTemplateId;eac96648-be59-4f36-0ba3-59117efc8138;"
        b"TargetMap;m_UID64;0;0;0;0102000000000000;"
        b"CardsToSacrifice;m_UID64;0;0;0;0103000000000000;"
    )
    command = SimpleNamespace(is_ability_activate=True)
    payload = typed_payload_from_decoded(command, {
        "__raw__": raw,
        "AbilityActivationData": {
            "TargetMap": {"0": [0x201]},
        },
    })
    activation = payload["activation_data"]
    assert activation["target_map"] == {0: [0x201]}
    assert activation["cost_target_map"] == {0: [0x301]}


def test_typed_activation_x_cost_data_maps_resource_cost():
    payload = typed_payload_from_decoded(None, {
        "m_AbilityActivationData": [{
            "xCostData": {"ResourceXCost": 3, "SetValues": 1},
        }],
    })
    activation = payload["activation_data"][0]
    assert activation["x_cost_data"]["resource_x_cost"] == 3
    assert activation["x_cost"] == 3


def test_typed_card_play_preserves_ability_data_and_free_flag():
    command = classify_player_transaction(b"PlaySpellTransaction")
    payload = typed_payload_from_decoded(command, {
        "m_SessionCardId": {"m_UID64": 0x101},
        "m_AbilityDataList": [{
            "OptedIn": True,
            "TargetMap": {"0": {"m_SessionCardIds": [{"m_UID64": 0x201}]}},
            "XCostData": {"ResourceXCost": 2},
        }],
        "m_PlayingForFree": True,
    })
    assert payload["card_id"] == 0x101
    assert payload["playing_for_free"] is True
    assert payload["ability_data"][0]["opted"] is True
    assert payload["ability_data"][0]["target_map"] == {0: [0x201]}
    assert payload["ability_data"][0]["x_cost"] == 2
    false_payload = typed_payload_from_decoded(command, {
        "m_SessionCardId": {"m_UID64": 0x101},
        "m_PlayingForFree": "0",
    })
    assert false_payload["playing_for_free"] is False


def test_typed_payload_extracts_named_session_checksum_data():
    command = classify_player_transaction(
        b"SendGameStateChecksumTransaction")
    payload = typed_payload_from_decoded(command, {
        "m_ChecksumData": {
            "SessionChecksum": 17,
            "WantData": False,
            "PlayerChecksums": {"player": 23},
        },
    })
    assert payload == {"checksum_data": {
        "session_checksum": 17,
        "want_data": False,
        "player_checksums": {"player": 23},
    }}


def test_typed_payload_extracts_encounter_conversation_id():
    command = classify_player_transaction(b"EncounterModDialogTransaction")
    payload = typed_payload_from_decoded(command, {
        "ConversationId": {"Guid": "conversation-guid"},
    })
    assert payload == {"conversation_id": "conversation-guid"}


def test_typed_payload_extracts_combat_declarations():
    attack = classify_player_transaction(b"CommitTroopsToAttackTransaction")
    assert typed_payload_from_decoded(attack, {
        "m_Attacks": [{"DefendingCardId": {"m_UID64": 11},
                        "AttackingCardIds": [{"m_UID64": 21}]}],
    })["declarations"] == ((11, (21,)),)
    defense = classify_player_transaction(b"CommitTroopsToDefenseTransaction")
    assert typed_payload_from_decoded(defense, {
        "m_DefenseDeclarations": [{"AttackerId": {"m_UID64": 31},
                                    "DefendingCardIds": [{"m_UID64": 41}]}],
    })["declarations"] == ((31, (41,)),)
    assert typed_payload_from_decoded(defense, {
        "m_DefenseDeclarations": [],
    }) == {"declarations": ()}


def test_typed_payload_extracts_turn_stops_and_auto_pass():
    stops = classify_player_transaction(b"SetTurnPhasesTransaction")
    assert typed_payload_from_decoded(stops, {
        "m_SelfTurnPhases": [{"value__": 4}],
        "m_OpponentTurnPhases": [{"value__": 7}],
    }) == {"self_phases": (4,), "opponent_phases": (7,)}
    auto = classify_player_transaction(b"SetAutoPassTransaction")
    assert typed_payload_from_decoded(auto, {
        "m_AsActive": True, "m_PassingState": {"value__": 2},
    }) == {"as_active": True, "passing_state": 2}


def test_typed_payload_preserves_quit_semantics():
    command = classify_player_transaction(
        b"QuitGameTransaction;m_WasBugged;0;0;0;True;"
        b"m_QuitEntireSeries;0;0;0;False;")
    assert typed_payload_from_decoded(command, {}) == {
        "was_bugged": True, "quit_entire_series": False}


def test_objfmt_dictionary_is_consumed_for_rules_port_payloads():
    from objfmt_builder import ObjFmtBuilder
    from hconnect_server import parse_datawrapper

    builder = ObjFmtBuilder("Test.Root")
    builder.begin_list(
        "TargetMap",
        "System.Collections.Generic.Dictionary`2#System.UInt64!System.String",
        1,
    )
    builder.add_dict_entry_uint64_str(0, 7, "selected")
    decoded = parse_datawrapper(builder.finish(1), preserve_complex=True)
    assert decoded["TargetMap"] == {7: "selected"}


def test_objfmt_session_card_id_list_is_consumed_for_rules_port_payloads():
    """A ``List<SessionCardId>`` must not be mistaken for a scalar card type.

    The collection's element type string contains ``SessionCardId``.  The
    generic scalar branch used to match it first with ``num == 0``, parse no
    members, and leave the cursor mid-element.  Corinth's charge-power choice
    serializes the picked card in exactly this shape, so the whole TargetMap
    was corrupted and the server saw no selection.
    """
    import struct
    from binascii import hexlify

    from objfmt_builder import ObjFmtBuilder
    from hconnect_server import parse_datawrapper

    builder = ObjFmtBuilder("Test.Root")
    list_idx, list_start = builder.begin_list(
        "m_SessionCardIds",
        "System.Collections.Generic.List`1#Game.Shared.SessionCardId",
        1,
    )
    el_idx = builder.begin_element(0, "Game.Shared.SessionCardId", 1)
    v_start = builder._buf.tell()
    v_idx = builder._push_size()
    vt = builder._add_type("Game.Shared.UID")
    builder._w("value"); builder._sep(); builder._w(str(v_idx)); builder._sep()
    builder._w(str(vt)); builder._sep(); builder._w("1"); builder._sep()
    sv_start = builder._buf.tell()
    sv_idx = builder._push_size()
    svt = builder._add_type("System.UInt64")
    builder._w("m_UID64"); builder._sep(); builder._w(str(sv_idx)); builder._sep()
    builder._w(str(svt)); builder._sep(); builder._w("0"); builder._sep()
    builder._w(hexlify(struct.pack("<Q", 0x271101)).decode("ascii"))
    builder._sep()
    builder._set_size(sv_idx, sv_start)
    builder._set_size(v_idx, v_start)
    builder._set_size(el_idx, builder._element_starts[el_idx])
    builder._set_size(list_idx, list_start)

    decoded = parse_datawrapper(builder.finish(1), preserve_complex=True)
    assert decoded["m_SessionCardIds"] == [{"value": {"m_UID64": 0x271101}}]


def _make_db():
    fd, path = tempfile.mkstemp(prefix="hex-application-", suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE game_sessions (
            session_id TEXT PRIMARY KEY,
            server_id TEXT,
            session_name TEXT UNIQUE,
            owner_uid TEXT,
            state TEXT,
            encounter_data TEXT,
            players_json TEXT,
            turn_order_json TEXT,
            seed_z INTEGER,
            seed_w INTEGER,
            deck_template_id TEXT,
            created_at TEXT
        )
    """)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value INTEGER)")
    conn.execute("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            gold INTEGER NOT NULL,
            platinum INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE emails (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            gold_delivered INTEGER,
            platinum_delivered INTEGER,
            attachments_json TEXT NOT NULL DEFAULT '[]',
            read_at TEXT,
            claimed_at TEXT
        )
    """)
    conn.execute("INSERT INTO users VALUES (1, 100, 20)")
    conn.execute(
        "INSERT INTO emails "
        "(id, user_id, gold_delivered, platinum_delivered, read_at, claimed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (10, 1, 25, 3, None, None),
    )
    conn.execute(
        "INSERT INTO game_sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("1", "2", "session-1", "244", "created", "{}",
         json.dumps([[500, 0]]), "[]", 1, 2, "", "2026-01-01"))
    conn.commit()
    conn.close()
    return path


def test_remove_session_commits_and_publishes_after_commit():
    path = _make_db()
    published = []
    try:
        dispatcher = ApplicationCommandDispatcher(
            event_publisher=published.extend,
            database_path=path,
        )
        result = dispatcher.execute(RemoveSessionCommand(500))

        assert result.value == "session-1"
        assert len(published) == 1
        assert isinstance(published[0], SessionRemoved)

        conn = sqlite3.connect(path)
        assert conn.execute("SELECT COUNT(*) FROM game_sessions").fetchone()[0] == 0
        conn.close()
    finally:
        os.unlink(path)


def test_transaction_rolls_back_on_failure():
    path = _make_db()
    try:
        try:
            with db.transaction(path) as conn:
                conn.execute("DELETE FROM game_sessions")
                raise RuntimeError("simulated command failure")
        except RuntimeError:
            pass

        conn = sqlite3.connect(path)
        assert conn.execute("SELECT COUNT(*) FROM game_sessions").fetchone()[0] == 1
        conn.close()
    finally:
        os.unlink(path)


def test_service_request_dispatch_passes_the_command_envelope():
    command = ServiceRequestCommand(
        target="ServiceGameSession",
        instance="1",
        data_type=3029,
        request_id=7,
        compressed=1,
        session_id="session-1",
        connection_handle="connection-1",
        inner_object={"__type__": "PassPriorityTransaction"},
        inner_bytes=b"payload",
    )
    received = []
    result = ApplicationCommandDispatcher.dispatch_request(
        command, lambda request: received.append(request) or "handled")

    assert result == "handled"
    assert received == [command]


def test_session_lifecycle_commands_share_one_transaction():
    path = _make_db()
    try:
        dispatcher = ApplicationCommandDispatcher(database_path=path)
        started = dispatcher.execute(StartSessionCommand("session-2", 700))
        session = started.value
        assert session.session_name == "session-2"
        assert session.players == [(700, 0)]

        joined = dispatcher.execute(JoinSessionCommand(session.session_id, 800))
        assert joined.value.players == [(700, 0), (800, 0)]

        changed = dispatcher.execute(SetSessionStateCommand(700, "setup"))
        assert changed.value.state == "setup"

        encounter = dispatcher.execute(StartEncounterCommand(
            "session-3", {"encounter": 1}, 900))
        assert encounter.value.encounter_data == {"encounter": 1}
    finally:
        os.unlink(path)


def test_player_transaction_classifier_is_side_effect_free_and_typed():
    raw = (b"PassPriorityTransaction;AcceptStartingHand;"
           b"m_TransactionId;0;0;0;0000000a;"
           b"m_QuitEntireSeries;0;0;0;False;")
    command = classify_player_transaction(raw)

    assert command.is_pass_priority is True
    assert command.is_mulligan_keep is True
    assert command.is_mulligan_redraw is False
    assert command.transaction_id == 10
    assert command.quit_series == "False"


def test_player_transaction_classifier_recognizes_triggered_ability_batch():
    command = classify_player_transaction(
        b"ActivateTriggeredAbiliesTransaction;ActivationData;"
        b"m_AbilityActivationData")
    assert command.is_activate_triggered_abilities is True
    assert command.is_ability_activate is False


def test_triggered_batch_normalizes_to_triggered_rules_intent():
    from rules_port.wire import normalize_player_transaction
    command = classify_player_transaction(
        b"ActivateTriggeredAbiliesTransaction;m_AbilityActivationData")
    command = command.__class__(**{
        **command.__dict__,
        "typed_payload": {"activation_data": ({"ability_instance_id": 4},)},
    })
    transaction = normalize_player_transaction(
        command, "p", current_phase=None,
        payload=command.typed_payload)
    assert transaction.kind == "activate_triggered_abilities"


def test_set_ability_data_without_instance_is_rejected_without_type_error():
    from types import SimpleNamespace
    from rules_port.wire import normalize_player_transaction
    command = SimpleNamespace(
        is_set_ability_data=True, is_ability_activate=False,
        is_activate_triggered_abilities=False, inner_bytes=b"",
        pass_turn_phase=None)
    assert normalize_player_transaction(
        command, "p", payload={"activation_data": {}}) is None


def test_triggered_flag_wins_when_wire_command_has_both_activation_flags():
    from types import SimpleNamespace
    from rules_port.wire import normalize_player_transaction
    command = SimpleNamespace(
        is_ability_activate=True,
        is_activate_triggered_abilities=True,
        is_set_ability_data=False,
        is_play_resource=False, is_play_troop=False,
        is_play_artifact=False, is_play_spell=False, is_play_champion=False,
        is_assign_damage=False, is_commit_attack=False,
        is_commit_defense=False, inner_bytes=b"",
        pass_turn_phase=None,
    )
    tx = normalize_player_transaction(
        command, "p", payload={"activation_data": ({"ability_instance_id": 4},)})
    assert tx.kind == "activate_triggered_abilities"


def test_player_transaction_classifier_recognizes_play_card_variants():
    command = classify_player_transaction(
        b"PlayTroopTransaction;PlayArtifactTransaction")
    assert command.is_play_troop is True
    assert command.is_play_artifact is True
    assert command.is_play_resource is False


def test_player_transaction_classifier_recognizes_control_transactions():
    command = classify_player_transaction(
        b"QuitGameTransaction;ReadyCardTransaction;"
        b"RequestPlayerOptionsTransaction;SendGameStateChecksumTransaction;"
        b"TipWindowClosed")
    assert command.is_quit_game and command.is_ready_card
    assert command.is_request_player_options and command.is_state_checksum
    assert command.is_tip_window_closed


def test_typed_attack_declaration_recovers_value_wrapped_session_cards():
    from application.player_transactions import typed_payload_from_decoded
    raw = (b";0;0;3;m_Attacks;4;4;0;1;0;5;5;2;"
           b"DefendingCardId;6;6;1;value;7;1;1;m_UID64;8;2;0;"
           b"0102000000000000;AttackingCardIds;9;7;0;1;0;10;6;1;"
           b"value;11;1;1;m_UID64;12;2;0;0106000000000000;"
           b"CommitTroopsToAttackTransaction")
    payload = typed_payload_from_decoded(None, {"__raw__": raw})
    assert payload["declarations"] == ((513, (1537,)),)


def test_empty_assign_damage_order_is_a_typed_noop():
    from types import SimpleNamespace
    command = SimpleNamespace(is_assign_damage=True)
    payload = typed_payload_from_decoded(
        command, {"AssignedDamageOrder": []})
    assert payload == {"assignments": ()}


def test_classifier_can_carry_decoder_owned_typed_payload():
    payload = {"card_id": 42}
    command = classify_player_transaction(b"PlayResourceTransaction",
                                          typed_payload=payload)
    assert command.typed_payload is payload


def test_mail_commands_commit_related_mutations_together():
    path = _make_db()
    try:
        dispatcher = ApplicationCommandDispatcher(database_path=path)
        dispatcher.execute(MarkMailReadCommand(1))
        claimed = dispatcher.execute(ClaimMailCommand(1, 10)).value
        assert claimed == {"gold": 25, "platinum": 3, "cards": []}

        conn = sqlite3.connect(path)
        user = conn.execute(
            "SELECT gold, platinum FROM users WHERE id=1").fetchone()
        email = conn.execute(
            "SELECT read_at, claimed_at FROM emails WHERE id=10").fetchone()
        assert user == (125, 23)
        assert email[0] is not None and email[1] is not None

        dispatcher.execute(DeleteMailCommand(1))
        assert conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 0
        conn.close()
    finally:
        os.unlink(path)


def main():
    test_ability_guid_is_extracted_without_objfmt_field_counting()
    test_remove_session_commits_and_publishes_after_commit()
    test_transaction_rolls_back_on_failure()
    test_service_request_dispatch_passes_the_command_envelope()
    test_session_lifecycle_commands_share_one_transaction()
    test_player_transaction_classifier_is_side_effect_free_and_typed()
    test_triggered_batch_normalizes_to_triggered_rules_intent()
    test_triggered_flag_wins_when_wire_command_has_both_activation_flags()
    test_typed_activation_target_instances_become_port_target_lists()
    test_typed_activation_x_cost_data_maps_resource_cost()
    test_typed_payload_extracts_encounter_conversation_id()
    test_typed_payload_extracts_combat_declarations()
    test_empty_assign_damage_order_is_a_typed_noop()
    test_typed_payload_extracts_turn_stops_and_auto_pass()
    test_typed_payload_preserves_quit_semantics()
    test_objfmt_dictionary_is_consumed_for_rules_port_payloads()
    test_objfmt_session_card_id_list_is_consumed_for_rules_port_payloads()
    test_mail_commands_commit_related_mutations_together()
    print("application transaction tests passed")


if __name__ == "__main__":
    main()
