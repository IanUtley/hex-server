"""ServiceProfile requests.

Moved out of hconnect_server.HCPHandler._handle_service_request_legacy.
"""

from application.response_encoding import compress_gzip
from application.response_encoding import encode_datawrapper
from application.response_encoding import encode_profile_response
from db import hexdump
from db import log_req
import json


# ServiceProfile (80000)
def handle_serviceprofile_80000(handler, target, instance, data_type, reqid, comp, session_id,
                                conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    envelope = inner_obj.get("Envelope", b"{}")
    _hc.log_req(f">>> ServiceProfile (dt=80000) env={len(envelope)}b")
    try:
        env_json = json.loads(envelope.decode("utf-8"))
        _hc.log_req(f"    Envelope action: {env_json.get('action', '?')}")
        for k, v in env_json.items():
            _hc.log_req(f"      {k}={str(v)[:80]}")
    except:
        _hc.log_req(f"    Envelope raw: {_hc.hexdump(envelope)}")
    try:
        env_json = json.loads(envelope.decode("utf-8"))
    except (TypeError, ValueError, UnicodeDecodeError):
        env_json = {}
    template_envelope = None
    if (isinstance(env_json, dict) and handler.user_profile
            and env_json.get("action") in ("pdecktsave", "pdeckdel")):
        # Profile deck templates (mercenary party decks).
        from services.deck_templates import handle_profile_action
        template_envelope = handle_profile_action(
            _hc._db, handler.user_profile["id"], env_json)
    if isinstance(env_json, dict) and env_json.get("action") == "qreplaylst":
        from replay import replay_list
        resp_envelope = json.dumps(
            replay_list(env_json), separators=(",", ":")
        ).encode("utf-8")
        _hc.log_req("    Replay list returned from game_replays")
    elif template_envelope is not None:
        resp_envelope = template_envelope
        _hc.log_req(f"    Deck template {env_json.get('action')} handled "
                    f"({len(resp_envelope)}b)")
    else:
        resp_envelope = b"{}"
    resp_inner = _hc.encode_profile_response(resp_envelope)
    resp_body = _hc.compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = _hc.encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
    issuer_str = f"0.0.0.0.ServiceProfile.{_hc.SERVICE_PROFILE_UID}.ServicePlayer.{handler.client_uid}.{resp_reqid}"
    handler.scnt += 1
    handler.send({
        "issuer": issuer_str,
        "target": target,
        "instance": instance,
        "reqid": resp_reqid,
        "c": comp,
        "conh": conh,
        "sid": handler.sid,
    }, dw_bytes)
    _hc.log_req(f"    Sent ServiceProfile response ({len(dw_bytes)}b)")



LEGACY_HANDLERS = {
    80000: handle_serviceprofile_80000,
}
