"""Mail and PingMailServer requests that the legacy service chain handled.

Moved out of hconnect_server.HCPHandler._handle_service_request_legacy.
"""

from application.response_encoding import compress_gzip
from application.response_encoding import encode_datawrapper
from application.response_encoding import encode_get_unread_mail_count_response
from application.response_encoding import encode_objfmt_response
from application.response_encoding import encode_ping_mail_server_response
from binascii import hexlify
from db import log_req
from profile_db import db_claim_mail_for_user
import io
import struct
import time


# GetUnreadMailCount (60007)
def handle_getunreadmailcount_60007(handler, target, instance, data_type, reqid, comp, session_id,
                                    conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    from profile_db import db_get_unread_mail_count
    count = db_get_unread_mail_count(handler.user_profile["id"]) if handler.user_profile else 0
    
    # First mail check after login = client is ready
    if handler._inventory_pending and handler.user_profile:
        handler._inventory_pending = False
        log_req(">>> Client ready (mail check received)")
    
    log_req(f">>> Respond GetUnreadMailCount -> {count}")
    resp_inner = encode_get_unread_mail_count_response(count)
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
    issuer_str = f"0.0.0.0.ServiceMail.{_hc.SERVICE_MAIL_UID}.ServicePlayer.{handler.client_uid}.{resp_reqid}"
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
    log_req(f"    Sent GetUnreadMailCount response ({len(dw_bytes)}b)")


# Mail.Receive (60002) — get mail list
def handle_get_mail_list_60002(handler, target, instance, data_type, reqid, comp, session_id,
                               conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    from profile_db import db_get_mail_list
    db_emails = db_get_mail_list(handler.user_profile["id"]) if handler.user_profile else []
    log_req(f">>> Respond Mail.Receive -> {len(db_emails)} emails")
    now = time.strftime("%m/%d/%Y %H:%M:%S", time.gmtime())
    
    # Include envelope type + UID type
    type_names = [
        "Game.Shared.Mail.Messages.Mail+Receive+Response",
        "Game.Shared.Mail.Messages.Mail+PagingResponse",
        "System.Collections.Generic.List`1#Game.Shared.Mail.Messages.Mail+Envelope",
        "Game.Shared.Mail.Messages.Mail+Envelope",
        "Game.Shared.UID",
        "System.UInt64",
        "System.String",
        "System.UInt32",
        "System.Int32",
        "System.DateTime",
    ]
    def ft(tn):
        if tn not in type_names:
            type_names.append(tn)
        return type_names.index(tn)
    
    sizes = []
    buf = io.BytesIO()
    w = lambda s: buf.write(s.encode("utf-8"))
    sep = lambda: buf.write(b";")
    lf = lambda: buf.write(b"\n")
    
    sizes.append(0)
    w(""); sep(); w("0"); sep(); w(str(ft(type_names[0]))); sep(); w("1"); sep()
    
    f1 = buf.tell(); sizes.append(0)
    w("PagingResp"); sep(); w("1"); sep(); w(str(ft(type_names[1]))); sep(); w("5"); sep()
    
    # Envelopes collection
    f2 = buf.tell(); sizes.append(0)
    w("Envelopes"); sep(); w("2"); sep(); w(str(ft(type_names[2]))); sep(); w("0"); sep()
    w(str(len(db_emails))); sep()
    
    for i, (eid, sender, subject, body, sent, gold_dlv, plat_dlv, claimed) in enumerate(db_emails):
        fe = buf.tell(); sizes.append(0)
        eidx = len(sizes) - 1
        w(str(i)); sep(); w(str(eidx)); sep(); w(str(ft(type_names[3]))); sep(); w("11"); sep()
        
        def write_email_field(ftype, name, val):
            f = buf.tell(); sizes.append(0)
            w(name); sep(); w(str(len(sizes)-1)); sep(); w(str(ft(ftype))); sep(); w("0"); sep()
            if ftype == "System.UInt64":
                w(hexlify(struct.pack("<Q", val)).decode("ascii")); sep()
            elif ftype == "System.String":
                enc = val.encode("utf-8")
                w(str(len(enc))); sep(); buf.write(enc)
            elif ftype == "System.UInt32":
                w(hexlify(struct.pack("<I", val)).decode("ascii")); sep()
            elif ftype == "System.Int32":
                w(hexlify(struct.pack("<i", val)).decode("ascii")); sep()
            elif ftype == "System.DateTime":
                enc = str(val).encode("utf-8")
                w(str(len(enc))); sep(); buf.write(enc)
            sizes[-1] = buf.tell() - f
        
        def write_uid_field(name, val):
            f = buf.tell(); sizes.append(0)
            w(name); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("Game.Shared.UID"))); sep(); w("1"); sep()
            fsub = buf.tell(); sizes.append(0)
            w("m_UID64"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.UInt64"))); sep(); w("0"); sep()
            w(hexlify(struct.pack("<Q", val)).decode("ascii")); sep()
            sizes[-1] = buf.tell() - fsub
            sizes[-2] = buf.tell() - f
        
        write_uid_field("MailID", 1000 + eid)
        write_uid_field("SenderID", 0)
        write_email_field("System.String", "SenderName", sender)
        write_uid_field("ReceiverID", int(handler.client_reck_id))
        write_email_field("System.String", "ReceiverName", handler.user_profile["name"])
        write_email_field("System.String", "Template", "")
        write_email_field("System.String", "Subject", subject)
        write_email_field("System.String", "Body", body or "")
        write_email_field("System.UInt32", "Platinum", plat_dlv or 0)
        write_email_field("System.UInt32", "Gold", gold_dlv or 0)
        write_email_field("System.DateTime", "Created", now)
    
        sizes[eidx] = buf.tell() - fe
    
    sizes[2] = buf.tell() - f2
    
    # MinTime
    f3 = buf.tell(); sizes.append(0)
    w("MinTime"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft(type_names[9]))); sep(); w("0"); sep()
    enc = now.encode("utf-8")
    w(str(len(enc))); sep(); buf.write(enc)
    sizes[-1] = buf.tell() - f3
    
    # MaxTime
    f4 = buf.tell(); sizes.append(0)
    w("MaxTime"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft(type_names[9]))); sep(); w("0"); sep()
    w(str(len(enc))); sep(); buf.write(enc)
    sizes[-1] = buf.tell() - f4
    
    # Offset
    f5 = buf.tell(); sizes.append(0)
    w("Offset"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft(type_names[7]))); sep(); w("0"); sep()
    w(hexlify(struct.pack("<I", 0)).decode("ascii")); sep()
    sizes[-1] = buf.tell() - f5
    
    # Total
    f6 = buf.tell(); sizes.append(0)
    w("Total"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft(type_names[7]))); sep(); w("0"); sep()
    w(hexlify(struct.pack("<I", len(db_emails))).decode("ascii")); sep()
    sizes[-1] = buf.tell() - f6
    
    sizes[1] = buf.tell() - f1
    sizes[0] = buf.tell()
    
    w(";".join(type_names))
    lf()
    for i, s in enumerate(sizes):
        if i > 0: w(";")
        w(str(s))
    
    resp_inner = buf.getvalue()
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
    issuer_str = f"0.0.0.0.ServiceMail.{_hc.SERVICE_MAIL_UID}.ServicePlayer.{handler.client_uid}.{resp_reqid}"
    handler.scnt += 1
    handler.send({
        "issuer": issuer_str, "target": target, "instance": instance,
        "reqid": resp_reqid, "c": comp, "conh": conh, "sid": handler.sid,
    }, dw_bytes)
    log_req(f"    Sent Mail.Receive response ({len(dw_bytes)}b)")


# Mail.Delivered (60005) — sent mail
def handle_sent_mail_60005(handler, target, instance, data_type, reqid, comp, session_id,
                           conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    log_req(">>> Respond Mail.Delivered -> 0 (empty)")
    now = time.strftime("%m/%d/%Y %H:%M:%S", time.gmtime())
    type_names = [
        "Game.Shared.Mail.Messages.Mail+Delivered+Response",
        "Game.Shared.Mail.Messages.Mail+PagingResponse",
        "System.Collections.Generic.List`1#Game.Shared.Mail.Messages.Mail+Envelope",
        "System.DateTime", "System.UInt32",
    ]
    def ft(tn):
        if tn not in type_names:
            type_names.append(tn)
        return type_names.index(tn)
    sizes = []
    buf = io.BytesIO()
    w = lambda s: buf.write(s.encode("utf-8"))
    sep = lambda: buf.write(b";")
    lf = lambda: buf.write(b"\n")
    sizes.append(0)
    w(""); sep(); w("0"); sep(); w(str(ft(type_names[0]))); sep(); w("1"); sep()
    f1 = buf.tell(); sizes.append(0)
    w("PagingResp"); sep(); w("1"); sep(); w(str(ft(type_names[1]))); sep(); w("5"); sep()
    f2 = buf.tell(); sizes.append(0)
    w("Envelopes"); sep(); w("2"); sep(); w(str(ft(type_names[2]))); sep(); w("0"); sep()
    w("0"); sep()
    sizes[2] = buf.tell() - f2
    f3 = buf.tell(); sizes.append(0)
    w("MinTime"); sep(); w("3"); sep(); w(str(ft(type_names[3]))); sep(); w("0"); sep()
    enc = now.encode("utf-8"); w(str(len(enc))); sep(); buf.write(enc)
    sizes[3] = buf.tell() - f3
    f4 = buf.tell(); sizes.append(0)
    w("MaxTime"); sep(); w("4"); sep(); w(str(ft(type_names[3]))); sep(); w("0"); sep()
    w(str(len(enc))); sep(); buf.write(enc)
    sizes[4] = buf.tell() - f4
    f5 = buf.tell(); sizes.append(0)
    w("Offset"); sep(); w("5"); sep(); w(str(ft(type_names[4]))); sep(); w("0"); sep()
    w(hexlify(struct.pack("<I", 0)).decode("ascii")); sep()
    sizes[5] = buf.tell() - f5
    f6 = buf.tell(); sizes.append(0)
    w("Total"); sep(); w("6"); sep(); w(str(ft(type_names[4]))); sep(); w("0"); sep()
    w(hexlify(struct.pack("<I", 0)).decode("ascii")); sep()
    sizes[6] = buf.tell() - f6
    sizes[1] = buf.tell() - f1
    sizes[0] = buf.tell()
    w(";".join(type_names)); lf()
    for i, s in enumerate(sizes):
        if i > 0: w(";")
        w(str(s))
    resp_inner = buf.getvalue()
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
    issuer_str = f"0.0.0.0.ServiceMail.{_hc.SERVICE_MAIL_UID}.ServicePlayer.{handler.client_uid}.{resp_reqid}"
    handler.scnt += 1
    handler.send({
        "issuer": issuer_str, "target": target, "instance": instance,
        "reqid": resp_reqid, "c": comp, "conh": conh, "sid": handler.sid,
    }, dw_bytes)
    log_req(f"    Sent Mail.Delivered response ({len(dw_bytes)}b)")


# Mail.MarkRead (60004)
def handle_mail_markread_60004(handler, target, instance, data_type, reqid, comp, session_id,
                               conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    log_req(">>> Respond Mail.MarkRead")
    from profile_db import db_mark_all_mail_read
    if handler.user_profile:
        db_mark_all_mail_read(handler.user_profile["id"])
    resp_inner = encode_objfmt_response(
        ["Game.Shared.Mail.Messages.Mail+MarkRead+Response"], []
    )
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
    issuer_str = f"0.0.0.0.ServiceMail.{_hc.SERVICE_MAIL_UID}.ServicePlayer.{handler.client_uid}.{resp_reqid}"
    handler.scnt += 1
    handler.send({
        "issuer": issuer_str, "target": target, "instance": instance,
        "reqid": resp_reqid, "c": comp, "conh": conh, "sid": handler.sid,
    }, dw_bytes)
    log_req(f"    Sent Mail.MarkRead response ({len(dw_bytes)}b)")


# Mail.MarkDelete (60006)
def handle_mail_markdelete_60006(handler, target, instance, data_type, reqid, comp, session_id,
                                 conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    log_req(">>> Respond Mail.MarkDelete")
    # Mark all user's emails as deleted in DB
    from profile_db import db_delete_all_mail
    if handler.user_profile:
        db_delete_all_mail(handler.user_profile["id"])
    resp_inner = encode_objfmt_response(
        ["Game.Shared.Mail.Messages.Mail+MarkDelete+Response"], []
    )
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
    issuer_str = f"0.0.0.0.ServiceMail.{_hc.SERVICE_MAIL_UID}.ServicePlayer.{handler.client_uid}.{resp_reqid}"
    handler.scnt += 1
    handler.send({
        "issuer": issuer_str, "target": target, "instance": instance,
        "reqid": resp_reqid, "c": comp, "conh": conh, "sid": handler.sid,
    }, dw_bytes)
    log_req(f"    Sent Mail.MarkDelete response ({len(dw_bytes)}b)")


# Mail.Claim (60003) — claim attachments from a mail
def handle_claim_attachments_from_a_mail_60003(handler, target, instance, data_type, reqid, comp, session_id,
                                               conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    log_req(">>> Mail.Claim (dt=60003)")
    p = handler.user_profile
    mail_id_obj = inner_obj.get("MailID", {})
    mail_id_64 = 0
    if isinstance(mail_id_obj, dict):
        mail_id_64 = mail_id_obj.get("m_UID64", 0)
    elif isinstance(mail_id_obj, int):
        mail_id_64 = mail_id_obj
    eid = mail_id_64 - 1000 if mail_id_64 else 0
    
    gold_granted = 0
    plat_granted = 0
    if eid > 0 and p:
        claim = db_claim_mail_for_user(p["id"], eid)
        if claim is not None:
            gold_granted = claim["gold"]
            plat_granted = claim["platinum"]
            if gold_granted or plat_granted:
                p["gold"] += gold_granted
                p["platinum"] += plat_granted
                log_req(f"    Claimed mail #{eid}: gold+{gold_granted} plat+{plat_granted}")
            if claim.get("cards"):
                handler.push_cards_to_client()
                log_req(f"    Claimed mail #{eid}: cards+{len(claim['cards'])}")
        else:
            log_req(f"    Mail #{eid} already claimed or not found")
    
    resp_inner = encode_objfmt_response(
        ["Game.Shared.Mail.Messages.Mail+Claim+Response",
         "System.UInt64",
         "System.UInt32",
         "System.Collections.Generic.List`1#Game.Shared.Mail.Messages.Mail+AttachmentCard",
         "System.Collections.Generic.List`1#Game.Shared.Mail.Messages.Mail+AttachmentInventory"],
        [("EnvCLID", "ulong", mail_id_64),
         ("CardC", "uint", 0),
         ("InvenC", "uint", 0),
         ("Cards", "coll", ("System.Collections.Generic.List`1#Game.Shared.Mail.Messages.Mail+AttachmentCard", 0)),
         ("Inven", "coll", ("System.Collections.Generic.List`1#Game.Shared.Mail.Messages.Mail+AttachmentInventory", 0))]
    )
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
    issuer_str = f"0.0.0.0.ServiceMail.{_hc.SERVICE_MAIL_UID}.ServicePlayer.{handler.client_uid}.{resp_reqid}"
    handler.scnt += 1
    handler.send_and_cache({
        "issuer": issuer_str, "target": target, "instance": instance,
        "reqid": resp_reqid, "c": comp, "conh": conh, "sid": handler.sid,
    }, dw_bytes, data_type, reqid, target, instance)
    log_req(f"    Sent Mail.Claim response ({len(dw_bytes)}b)")


# PingMailServer (9001)
def handle_pingmailserver_9001(handler, target, instance, data_type, reqid, comp, session_id,
                               conh, inner_obj, inner_bytes, raw_data_wrapper_bytes):
    import hconnect_server as _hc
    log_req(">>> Respond PingMailServer (dt=9001)")
    timestamp = time.strftime("%m/%d/%Y %H:%M:%S", time.gmtime())
    resp_inner = encode_ping_mail_server_response(timestamp)
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp, session_id)
    issuer_str = f"0.0.0.0.ServiceMail.{_hc.SERVICE_MAIL_UID}.ServicePlayer.{handler.client_uid}.{resp_reqid}"
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
    log_req(f"    Sent PingMailServer response ({len(dw_bytes)}b)")



LEGACY_HANDLERS = {
    60007: handle_getunreadmailcount_60007,
    60002: handle_get_mail_list_60002,
    60005: handle_sent_mail_60005,
    60004: handle_mail_markread_60004,
    60006: handle_mail_markdelete_60006,
    60003: handle_claim_attachments_from_a_mail_60003,
    9001: handle_pingmailserver_9001,
}
