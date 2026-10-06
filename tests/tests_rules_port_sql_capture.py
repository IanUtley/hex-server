import sqlite3
import pathlib
import sys
import os
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tests.test_db import fresh_database

fresh_database()   # bind this process's database before ``db`` is imported

from rules_port.sql_capture import (capture_session, event_capture,
                                    packet_capture, transaction_capture)


def main():
    db = sqlite3.connect(":memory:")
    db.executescript("""
      create table session_transactions (
        id integer, session_id text, player_uid integer, received_seq integer,
        received_at text, data_type integer, request_id integer, compressed integer,
        transaction_id integer, transaction_type text, classification_json text,
        decoded_intent_json text, raw_request_body blob, inner_bytes blob, pre_state_hash text,
        pre_state_json text, pre_rules_port_json text, post_state_hash text,
        post_state_json text, post_rules_port_json text, status text,
        handled integer, error text);
      create table session_events (
        id integer, session_id text, target_player_uid integer, seq integer,
        event_class integer, event_bytes blob, sent_at text);
      create table session_packets (
        id integer, session_id text, target_player_uid integer, seq integer,
        data_type integer, request_id integer, transaction_id integer,
        transaction_row_id integer, event_count integer, event_batch_json text,
        projection_recorded integer default 1,
        send_count integer default 0, sent_body blob, sent_at text,
        captured_at text);
    """)
    db.execute(
        "insert into session_transactions "
        "(id,session_id,player_uid,received_seq,received_at,data_type,"
        "request_id,compressed,transaction_id,transaction_type,"
        "classification_json,decoded_intent_json,raw_request_body,inner_bytes,"
        "pre_state_hash,pre_state_json,pre_rules_port_json,post_state_hash,"
        "post_state_json,post_rules_port_json,status,handled,error) "
        "values (1,'s',7,2,'now',3029,9,1,3,'T',"
        "'{\"is_pass_priority\":true}','{\"card_id\":42}',x'090807',"
        "x'0102','a','{\"cards\":[]}','{\"phase\":\"pre\"}','b',"
        "'{\"cards\":[42]}','{\"phase\":\"post\"}','completed',1,'')")
    db.execute("insert into session_events values (2,'s',7,4,65,x'0304','now')")
    db.execute(
        "insert into session_packets "
        "(id,session_id,target_player_uid,seq,data_type,request_id,"
        "transaction_id,transaction_row_id,event_count,event_batch_json,"
        "captured_at) values (3,'s',7,5,3055,9,3,1,0,'[]','now')")
    tx = transaction_capture(db, "s")
    assert tx[0]["classification"]["is_pass_priority"] is True
    assert tx[0]["decoded_intent"] == {"card_id": 42}
    assert tx[0]["raw_request_body"] == b"\x09\x08\x07"
    assert tx[0]["inner_bytes"] == b"\x01\x02"
    assert tx[0]["pre_state"] == {"cards": []}
    assert tx[0]["post_state"] == {"cards": [42]}
    assert tx[0]["pre_rules_port_state"] == {"phase": "pre"}
    assert tx[0]["post_rules_port_state"] == {"phase": "post"}
    ev = event_capture(db, "s")
    assert ev[0]["event_bytes"] == b"\x03\x04"
    packets = packet_capture(db, "s", transaction_row_id=1)
    assert packets[0]["event_count"] == 0
    assert packets[0]["transaction_id"] == 3
    packed = capture_session(db, "s")
    assert packed["transactions"][0]["inner_bytes"] == "0102"
    assert packed["transactions"][0]["raw_request_body"] == "090807"
    assert packed["events"][0]["event_bytes"] == "0304"
    assert packed["packets"][0]["events"] == []

    # Exercise the runtime event hook with a no-event packet. This proves the
    # packet ledger keeps the 3055 acknowledgement and ties it to the active
    # request context even though session_events has no rows to store.
    import db as db_layer
    import game_engine
    import replay_db
    from replay_db import db_mark_session_packet_sent
    from rules_port.trace import _card_state, _cards

    trace_db = db_layer._db
    trace_db.execute(
        "INSERT INTO game_cards (user_id, session_id, card_uid, "
        "card_template_id, gems, original_gems) VALUES (5, 98765, 258, 1, 8, NULL)"
    )
    card_before = _card_state(_cards(trace_db, 98765)[258])
    assert card_before["gems"] == 8
    assert card_before["original_gems"] is None
    trace_db.execute(
        "UPDATE game_cards SET gems=32, original_gems=8 "
        "WHERE session_id=98765 AND card_uid=258"
    )
    card_after = _card_state(_cards(trace_db, 98765)[258])
    assert card_after["gems"] == 32
    assert card_after["original_gems"] == 8
    transaction_row_id = replay_db.db_record_session_transaction(
        "98765", 7, 9, 3029, 0, 3, "PassPriorityTransaction",
        {"is_pass_priority": True}, b"\x01\x02", "pre-hash",
        decoded_intent={"kind": "pass_priority"},
        raw_data_wrapper_bytes=b"raw client DataWrapper",
        pre_state_json='{"cards":[]}',
        pre_rules_port_json='{"phase":"main"}')
    old_capture_setting = os.environ.get("HEX_CAPTURE_SESSION_TRACES")
    os.environ["HEX_CAPTURE_SESSION_TRACES"] = "1"
    try:
        with replay_db.session_transaction_trace(9):
            replay_db.set_session_transaction_trace_ids(
                transaction_id=3, transaction_row_id=transaction_row_id,
                session_id="98765")
            replay_db._record_session_events(
                game_engine.UID(98765), game_engine.UID(7), [])
            assert db_mark_session_packet_sent(
                "98765", 7, b"captured 3055 packet")
    finally:
        if old_capture_setting is None:
            os.environ.pop("HEX_CAPTURE_SESSION_TRACES", None)
        else:
            os.environ["HEX_CAPTURE_SESSION_TRACES"] = old_capture_setting
    replay_db.db_complete_session_transaction(
        transaction_row_id, "post-hash", True,
        post_state_json='{"cards":[42]}',
        post_rules_port_json='{"phase":"combat"}')
    recorded_packet = trace_db.execute(
        "SELECT data_type, request_id, transaction_id, transaction_row_id, "
        "event_count, event_batch_json, send_count, sent_body "
        "FROM session_packets "
        "WHERE session_id='98765' ORDER BY id DESC LIMIT 1").fetchone()
    assert recorded_packet == (
        3055, 9, 3, transaction_row_id, 0, "[]", 1,
        b"captured 3055 packet")
    sent_packet = packet_capture(trace_db, "98765")[0]
    assert sent_packet["projection_recorded"] is True
    assert sent_packet["sent_body"] == b"captured 3055 packet".hex()

    # Some host paths construct a 3055 directly rather than through the
    # shared event projection callback. Preserve the successful send count and
    # exact body while distinguishing that packet from a complete projection.
    direct_transaction_row_id = replay_db.db_record_session_transaction(
        "98766", 7, 10, 3029, 0, 4, "PassPriorityTransaction",
        {"is_pass_priority": True}, b"\x03\x04", "pre-hash")
    with replay_db.session_transaction_trace(10):
        replay_db.set_session_transaction_trace_ids(
            transaction_id=4,
            transaction_row_id=direct_transaction_row_id,
            session_id="98766")
        assert db_mark_session_packet_sent("98766", 7, b"direct 3055 packet")
    direct_packet = packet_capture(trace_db, "98766")[0]
    assert direct_packet["projection_recorded"] is False
    assert direct_packet["send_count"] == 1
    assert direct_packet["sent_body"] == b"direct 3055 packet".hex()

    failed_transaction_row_id = replay_db.db_record_session_transaction(
        "98767", 7, 11, 3029, 0, 5, "PassPriorityTransaction",
        {"is_pass_priority": True}, b"\x05\x06", "pre-hash",
        raw_data_wrapper_bytes=b"failing client DataWrapper")
    try:
        with replay_db.session_transaction_trace(11):
            replay_db.set_session_transaction_trace_ids(
                transaction_id=5,
                transaction_row_id=failed_transaction_row_id,
                session_id="98767")
            raise ValueError("synthetic resolver failure")
    except ValueError as error:
        assert str(error) == "synthetic resolver failure"
    else:
        raise AssertionError("transaction exception was swallowed")
    failed_capture = trace_db.execute(
        "SELECT status, handled, error FROM session_transactions WHERE id=?",
        (failed_transaction_row_id,)).fetchone()
    assert failed_capture == (
        "error", 0, "ValueError: synthetic resolver failure")

    runtime_transaction = transaction_capture(trace_db, "98765")[0]
    assert runtime_transaction["decoded_intent"] == {"kind": "pass_priority"}
    assert runtime_transaction["raw_request_body"] == b"raw client DataWrapper"
    assert runtime_transaction["pre_state"] == {"cards": []}
    assert runtime_transaction["pre_rules_port_state"] == {"phase": "main"}
    assert runtime_transaction["post_state"] == {"cards": [42]}
    assert runtime_transaction["post_rules_port_state"] == {"phase": "combat"}

    from application.player_transactions import classify_player_transaction
    command = classify_player_transaction(
        b"PassPriorityTransaction;m_TransactionId;0;0;0;0a000000;")
    assert command.transaction_id == 10

    from rules_port.trace_bundle import (_client_acceptance,
                                        build_trace_bundle)
    unity_state_before = (
        "Debug: UIBattle|10/3/2026 9:22:16 AM|"
        "Pushing UI state: BattleStateMain")
    unity_line = (
        "Debug: ClientSessionBase|10/3/2026 9:22:17 AM|Player 7 "
        "SUBMITTING TRANSACTION 3: PassPriorityTransaction (Player 7)")
    unity_state_after = (
        "Debug: UIBattle|10/3/2026 9:22:18 AM|"
        "Popping UI state: BattleStateMain")
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as client_log:
        client_log.write("\n".join((unity_state_before, unity_line,
                                     unity_state_after)) + "\n")
        client_log.flush()
        bundle = build_trace_bundle(
            db, "s", 1, client_log.name,
            expected_ui_states=("Pushing UI state: BattleStateMain",
                                "Popping UI state: BattleStateMain"))
        incomplete = build_trace_bundle(
            db, "s", 1, client_log.name,
            expected_ui_states=("Pushing UI state: MissingState",))
    assert bundle["correlation"] == "transaction_id"
    assert bundle["projected_packet_batch_count"] == 1
    assert bundle["socket_send_count"] == 0
    assert bundle["unconfirmed_projection_count"] == 1
    assert bundle["send_without_projection_count"] == 0
    assert bundle["packets"][0]["events"] == []
    assert bundle["client_acceptance"]["status"] == "pass"
    assert incomplete["client_acceptance"]["status"] == "fail"
    time_window_only = _client_acceptance(
        {"match": "time_window_only",
         "lines": [unity_state_before, unity_state_after]},
        ("Pushing UI state: BattleStateMain",
         "Popping UI state: BattleStateMain"),
        allow_time_window=True)
    assert time_window_only["status"] == "pass_time_window_only"
    print("PASS rules-port SQL capture tests")


if __name__ == "__main__":
    main()
