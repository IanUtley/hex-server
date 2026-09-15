"""Registry-backed bridge from HConnect service requests to handlers."""

import importlib

from application.commands import ServiceRequestCommand
from services import dispatch as service_dispatch


def dispatch_service(handler, data_type, target, instance, reqid, comp,
                     session_id, conh, inner_obj, inner_bytes,
                     *, service_uids, log_req):
    entry = service_dispatch(data_type)
    if not entry:
        return False
    mod_name, fn_name, extra_kw = entry
    fn = getattr(importlib.import_module(mod_name), fn_name)
    kwargs = {
        "SERVICE_MAIL_UID": service_uids["mail"],
        "SERVICE_PROFILE_UID": service_uids["profile"],
        "log_req": log_req,
    }
    kwargs.update(extra_kw)
    command = ServiceRequestCommand(
        target=target, instance=instance, data_type=data_type,
        request_id=reqid, compressed=comp, session_id=session_id,
        connection_handle=conh, inner_object=inner_obj, inner_bytes=inner_bytes,
    )
    handler._application.dispatch_request(
        command,
        lambda request: fn(
            handler, request.target, request.instance, request.request_id,
            request.compressed, request.session_id, request.connection_handle,
            inner_obj=request.inner_object, inner_bytes=request.inner_bytes,
            **kwargs),
    )
    return True
