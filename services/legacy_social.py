"""Friend and social requests.

Moved out of hconnect_server.HCPHandler._handle_service_request_legacy.
"""


# --- Friend / Social ---
def handle_friend_social_2149(handler, target, instance, data_type, reqid, comp, session_id,
                              conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    from services.social import handle_add_friend
    handle_add_friend(handler, target, instance, reqid, comp, session_id,
                      conh, inner_obj, inner_bytes,
                      SERVICE_PROFILE_UID=_hc.SERVICE_PROFILE_UID)


def handle_request_2157(handler, target, instance, data_type, reqid, comp, session_id,
                        conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    from services.social import handle_accept_friend_request
    handle_accept_friend_request(handler, target, instance, reqid, comp,
                                 session_id, conh, inner_obj, inner_bytes,
                                 SERVICE_PROFILE_UID=_hc.SERVICE_PROFILE_UID)


def handle_request_2159(handler, target, instance, data_type, reqid, comp, session_id,
                        conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    from services.social import handle_ignore_friend_request
    handle_ignore_friend_request(handler, target, instance, reqid, comp,
                                 session_id, conh, inner_obj, inner_bytes,
                                 SERVICE_PROFILE_UID=_hc.SERVICE_PROFILE_UID)


def handle_request_2161(handler, target, instance, data_type, reqid, comp, session_id,
                        conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    from services.social import handle_remove_friend
    handle_remove_friend(handler, target, instance, reqid, comp, session_id,
                         conh, inner_obj, inner_bytes,
                         SERVICE_PROFILE_UID=_hc.SERVICE_PROFILE_UID)


def handle_request_2163(handler, target, instance, data_type, reqid, comp, session_id,
                        conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    from services.social import handle_ignore_player
    handle_ignore_player(handler, target, instance, reqid, comp, session_id,
                         conh, inner_obj, inner_bytes,
                         SERVICE_PROFILE_UID=_hc.SERVICE_PROFILE_UID)


def handle_request_2165(handler, target, instance, data_type, reqid, comp, session_id,
                        conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    from services.social import handle_unignore_player
    handle_unignore_player(handler, target, instance, reqid, comp, session_id,
                           conh, inner_obj, inner_bytes,
                           SERVICE_PROFILE_UID=_hc.SERVICE_PROFILE_UID)



LEGACY_HANDLERS = {
    2149: handle_friend_social_2149,
    2157: handle_request_2157,
    2159: handle_request_2159,
    2161: handle_request_2161,
    2163: handle_request_2163,
    2165: handle_request_2165,
}
