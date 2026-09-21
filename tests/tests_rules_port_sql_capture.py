import sqlite3
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tests.test_db import fresh_database

fresh_database()   # bind this process's database before ``db`` is imported

from rules_port.sql_capture import capture_session, event_capture, transaction_capture


def main():
    db = sqlite3.connect(":memory:")
    db.executescript("""
      create table session_transactions (
        id integer, session_id text, player_uid integer, received_seq integer,
        received_at text, data_type integer, request_id integer, compressed integer,
        transaction_id integer, transaction_type text, classification_json text,
        inner_bytes blob, pre_state_hash text, post_state_hash text, status text,
        handled integer, error text);
      create table session_events (
        id integer, session_id text, target_player_uid integer, seq integer,
        event_class integer, event_bytes blob, sent_at text);
    """)
    db.execute("insert into session_transactions values (1,'s',7,2,'now',3029,9,1,3,'T','{\"is_pass_priority\":true}',x'0102','a','b','completed',1,'')")
    db.execute("insert into session_events values (2,'s',7,4,65,x'0304','now')")
    tx = transaction_capture(db, "s")
    assert tx[0]["classification"]["is_pass_priority"] is True
    assert tx[0]["inner_bytes"] == b"\x01\x02"
    ev = event_capture(db, "s")
    assert ev[0]["event_bytes"] == b"\x03\x04"
    packed = capture_session(db, "s")
    assert packed["transactions"][0]["inner_bytes"] == "0102"
    assert packed["events"][0]["event_bytes"] == "0304"
    print("PASS rules-port SQL capture tests")


if __name__ == "__main__":
    main()
