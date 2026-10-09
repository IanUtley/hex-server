"""ServiceCampaign requests (Siege, Main campaign, cluster picks).

Moved out of hconnect_server.HCPHandler._handle_service_request_legacy.
"""

from application.response_encoding import compress_gzip
from application.response_encoding import encode_datawrapper
from application.response_encoding import encode_objfmt_response
from db import _db
from db import log_req
import campaign
import json


# ServiceCampaign Siege (150000)
def handle_servicecampaign_siege_150000(handler, target, instance, data_type, reqid, comp, session_id,
                                        conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    envelope = inner_obj.get("Envelope", b"{}")
    try:
        env_json = json.loads(envelope) if isinstance(envelope, (bytes, str)) else envelope
        log_req(f">>> Campaign Siege (dt=150000) action={env_json.get('RequestType', '?')}")
    except:
        log_req(f">>> Campaign Siege (dt=150000) raw={envelope[:80]}")
    resp_envelope = json.dumps({"SiegeStatus": 2, "Offset": 0, "Count": 0, "Results": []}).encode("utf-8")
    resp_inner = encode_objfmt_response(
        ["Game.Shared.Campaign.Siege.Messaging+Response",
         "System.Byte[]"],
        [("Envelope", "bytes", resp_envelope)]
    )
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
    issuer_str = f"0.0.0.0.ServiceCampaign.{_hc.SERVICE_MAIL_UID}.ServicePlayer.{handler.client_uid}.{resp_reqid}"
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
    log_req(f"    Sent Campaign Siege response ({len(dw_bytes)}b)")


# ServiceCampaign Main (110000) — campaign/adventure system
def handle_campaign_adventure_system_110000(handler, target, instance, data_type, reqid, comp, session_id,
                                            conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    campaign.handle_campaign_request(handler, _db, inner_obj, comp,
                                       session_id, reqid,
                                       target, instance, conh,
                                       _hc.SERVICE_MAIL_UID)


# Cluster pick (140000) — resolve service UIDs
def handle_resolve_service_uids_140000(handler, target, instance, data_type, reqid, comp, session_id,
                                       conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    log_req(f">>> Cluster pick (dt=140000) target={target}")
    # UID format in C#: [FieldOffset(0)] public ulong m_UID64 = type | (instance << 8)
    if target == "ServiceCampaign":
        service_uid = _hc.UID_TYPE["ServiceCampaign"]  # 253
        log_req(f"    Returning ServiceCampaign UID: {service_uid}")
    else:
        service_uid = _hc.UID_TYPE["ServiceGameSession"]  # 246
        log_req(f"    Returning ServiceGameSession UID: {service_uid}")
    env_json = json.dumps({"Kind": "pickres", "ServiceID": {"m_UID64": service_uid}})
    resp_inner = encode_objfmt_response(
        ["Game.Shared.Cluster.ClusterComms+EnvelopeR", "System.Byte[]"],
        [("Data", "bytes", env_json.encode("utf-8"))])
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
    issuer_str = f"0.0.0.0.ServiceProfile.{_hc.SERVICE_PROFILE_UID}.ServicePlayer.{handler.client_uid}.{resp_reqid}"
    handler.scnt += 1
    handler.send({"issuer": issuer_str, "target": target, "instance": instance,
        "reqid": resp_reqid, "c": comp, "conh": conh, "sid": handler.sid}, dw_bytes)
    log_req(f"    Sent Cluster pick response ({len(dw_bytes)}b)")



LEGACY_HANDLERS = {
    150000: handle_servicecampaign_siege_150000,
    110000: handle_campaign_adventure_system_110000,
    140000: handle_resolve_service_uids_140000,
}
