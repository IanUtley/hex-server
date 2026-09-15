"""Compatibility response-encoding facade for HConnect callers."""

import encoder
from profile_db import db_get_store_items


def encode_get_store_items_response():
    return encode_store_response(db_get_store_items())


def encode_store_response(items):
    return encoder.encode_store_response(items)


def encode_store_item_set1_booster():
    return encode_get_store_items_response()


def encode_objfmt_response(type_names, fields):
    return encoder.encode_objfmt_response(type_names, fields)


def encode_objfmt_string(value):
    return encoder.encode_objfmt_string(value)


def encode_session_state(session_id, session_name, min_players=2, max_players=2):
    return encoder.encode_session_state(session_id, session_name, min_players, max_players)


def encode_sync_event(packet):
    return encoder.encode_sync_event(packet)


def encode_challenger_list(challengers):
    return encoder.encode_challenger_list(challengers)


def encode_get_challengers_response(success, challengers):
    return encoder.encode_get_challengers_response(success, challengers)


def encode_login_stream_done():
    return encoder.encode_login_stream_done()


def encode_datawrapper(request_id, data_type, body_bytes, comp,
                       session_guid="00000000-0000-0000-0000-000000000000",
                       conh=0):
    return encoder.encode_datawrapper(
        request_id, data_type, body_bytes, comp, session_guid, conh)


def encode_get_unread_mail_count_response(unread_count=0):
    return encoder.encode_get_unread_mail_count_response(unread_count)


def encode_ping_mail_server_response(timestamp=None):
    return encoder.encode_ping_mail_server_response(timestamp)


def encode_profile_response(envelope_bytes):
    return encoder.encode_profile_response(envelope_bytes)


def compress_gzip(data):
    return encoder.compress_gzip(data)


def decompress_gzip(data):
    return encoder.decompress_gzip(data)


def make_uid(type_byte, instance_id):
    return encoder.make_uid(type_byte, instance_id)


def client_session_guid(handler):
    return getattr(
        handler, "client_req_session_id",
        "00000000-0000-0000-0000-000000000000")
