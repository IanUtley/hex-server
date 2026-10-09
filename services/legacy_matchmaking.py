"""Matchmaking ping and challenge requests.

Moved out of hconnect_server.HCPHandler._handle_service_request_legacy.
"""

from db import log_req


# Matchmaking — Ping (4001)
def handle_ping_4001(handler, target, instance, data_type, reqid, comp, session_id,
                     conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    from services.matchmaking import handle_ping_matchmaking
    handle_ping_matchmaking(handler, target, instance, reqid, comp,
                            session_id, conh, inner_obj, inner_bytes,
                            log_req=_hc.log_req)


# Matchmaking — SendQuickMatchChallenge (4013)
def handle_sendquickmatchchallenge_4013(handler, target, instance, data_type, reqid, comp, session_id,
                                        conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    from services.matchmaking import handle_send_quick_match_challenge
    handle_send_quick_match_challenge(handler, target, instance, reqid, comp,
                                      session_id, conh, inner_obj, inner_bytes,
                                      log_req=_hc.log_req)


# Matchmaking — SendChallengeResponse (4017)
def handle_sendchallengeresponse_4017(handler, target, instance, data_type, reqid, comp, session_id,
                                      conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    from services.matchmaking import handle_send_challenge_response
    handle_send_challenge_response(handler, target, instance, reqid, comp,
                                   session_id, conh, inner_obj, inner_bytes,
                                   log_req=_hc.log_req)



LEGACY_HANDLERS = {
    4001: handle_ping_4001,
    4013: handle_sendquickmatchchallenge_4013,
    4017: handle_sendchallengeresponse_4017,
}
