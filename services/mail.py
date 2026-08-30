"""Mail service handlers: inbox, sent, mark-read, delete, claim, unread count."""

import io, struct, sys, time
from binascii import hexlify

import db as _db_mod
from db import _db, db_get_unread_mail_count, log_req
from db import (db_delete_sent_mail, db_find_mail_recipient,
                db_get_sent_mail_list, db_send_email, display_name_from_identity)
from application.commands import (ClaimMailCommand, DeleteMailCommand,
                                  MarkMailReadCommand)
from encoder import encode_objfmt_response, compress_gzip, encode_datawrapper
from objfmt_builder import ObjFmtBuilder


def _send_response(handler, data_type, resp_inner, comp, session_id,
                   reqid, target, instance, conh,
                   service_name, SERVICE_MAIL_UID):
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body,
                                  comp, session_id)
    issuer_str = (f"0.0.0.0.{service_name}.{SERVICE_MAIL_UID}."
                  f"ServicePlayer.{handler.client_uid}.{resp_reqid}")
    handler.scnt += 1
    handler.send({
        "issuer": issuer_str, "target": target, "instance": instance,
        "reqid": resp_reqid, "c": comp, "conh": conh, "sid": handler.sid,
    }, dw_bytes)
    return len(dw_bytes)


def _server_module():
    return (sys.modules.get("hconnect_server") or sys.modules.get("__main__"))


def push_unread_notification(handler, count=None):
    """Push one 9005 event per unread mail to initialize the mail counter."""
    if not handler.user_profile:
        return
    if count is None:
        count = db_get_unread_mail_count(handler.user_profile["id"])
    count = max(0, int(count or 0))
    if not count:
        return

    server = _server_module()
    inner = encode_objfmt_response(
        ["Game.Shared.Network.Mail.NewMailReceivedEventArgs"], [])
    compressed = compress_gzip(inner)
    for _ in range(count):
        dw = encode_datawrapper(
            0, 9005, compressed, 1,
            "00000000-0000-0000-0000-000000000000")
        issuer = (
            f"0.0.0.0.ServiceMail.{server.SERVICE_MAIL_UID}."
            f"ServicePlayer.{handler.client_uid}.{handler.scnt}")
        handler.scnt += 1
        handler.send({
            "issuer": issuer, "target": "ServiceMail", "instance": "Shared",
            "reqid": 0, "c": 0, "conh": 0, "sid": handler.sid,
        }, dw)
    log_req(f">>> PUSH new-mail event dt=9005 ({count} unread)")


def notify_new_mail(user_id):
    """Notify all connected sessions for a newly delivered message."""
    server = _server_module()
    for handler, _last_active in list(server._active_clients.get(int(user_id), [])):
        push_unread_notification(handler, count=1)


def handle_get_unread(handler, target, instance, reqid, comp, session_id,
                      conh, SERVICE_MAIL_UID, **_kw):
    """GetUnreadMailCount (60007)."""
    if handler.user_profile:
        count = _db.execute(
            "SELECT COUNT(*) FROM emails WHERE user_id=? AND read_at IS NULL",
            (handler.user_profile["id"],)).fetchone()[0]
    else:
        count = 0
    # First mail check after login = client is ready
    if getattr(handler, '_inventory_pending', False) and handler.user_profile:
        handler._inventory_pending = False
        log_req(">>> Client ready (mail check received)")
    log_req(f">>> Respond GetUnreadMailCount -> {count}")
    from hconnect_server import encode_get_unread_mail_count_response
    inner = encode_get_unread_mail_count_response(count)
    sz = _send_response(handler, 60007, inner, comp, session_id,
                        reqid, target, instance, conh, "ServiceMail",
                        SERVICE_MAIL_UID)
    log_req(f"    Sent GetUnreadMailCount response ({sz}b)")


def _extract_mail_string(inner_bytes, field_name):
    """Read a length-prefixed string field from a Mail.Envelope payload."""
    if not isinstance(inner_bytes, bytes):
        return ""
    # A length-prefixed string is not followed by a separator; the next
    # field name starts immediately after the string bytes.  Match the field
    # name itself rather than requiring a leading semicolon.
    marker = field_name.encode("utf-8") + b";"
    position = inner_bytes.find(marker)
    if position < 0:
        return ""
    rest = inner_bytes[position + len(marker):]
    parts = rest.split(b";", 4)
    if len(parts) < 5:
        return ""
    try:
        value_length = int(parts[3])
    except ValueError:
        return ""
    value = parts[4][:value_length]
    return value.decode("utf-8", errors="replace")


def _encode_mail_send_response():
    b = ObjFmtBuilder("Game.Shared.Mail.Messages.Mail+Send+Response")
    b.begin_list(
        "Responses",
        "System.Collections.Generic.List`1#Game.Shared.Mail.Messages.Mail+MailResponse",
        0,
    )
    return b.finish(1)


def _encode_mail_error(error_code, message):
    b = ObjFmtBuilder("Game.Shared.Network.ErrorArgs")
    b.field_int("Error", error_code)
    b.field_str("ErrorMessage", message)
    return b.finish(2)


def _mail_uid64(email_id):
    """Encode a database email id as the client's UID.Type.Mail value."""
    return ((1000 + int(email_id)) << 8) | 9


def _mail_id_from_uid64(uid64):
    """Convert a Mail UID back to a database email id.

    Accept the old untyped ``1000 + id`` form as well, so existing clients or
    cached responses remain usable while new responses use UID.Type.Mail.
    """
    uid64 = int(uid64 or 0)
    if uid64 & 0xff == 9 and (uid64 >> 8) >= 1000:
        return (uid64 >> 8) - 1000
    return uid64 - 1000 if uid64 >= 1000 else 0


def _extract_mail_uid_list(inner_bytes, field_name="IDs"):
    """Extract UID values from a simple ObjFmt List<UID> field."""
    if not isinstance(inner_bytes, bytes):
        return []
    raw = inner_bytes.split(b"\n", 1)[0]
    marker = field_name.encode("utf-8") + b";"
    start = raw.find(marker)
    if start < 0:
        return []
    tokens = raw[start + len(marker):].split(b";")
    cursor = 0

    def take():
        nonlocal cursor
        if cursor >= len(tokens):
            raise ValueError("incomplete ObjFmt list")
        token = tokens[cursor]
        cursor += 1
        return token

    try:
        take()  # field size index
        take()  # field type index
        num_props = int(take())
        if num_props != 0:
            return []
        count = int(take())
        values = []
        for _ in range(count):
            take()  # element name/index
            take()  # element size index
            take()  # element type index
            element_props = int(take())
            for _ in range(element_props):
                nested_name = take()
                take()  # nested size index
                take()  # nested type index
                nested_props = int(take())
                if nested_name == b"m_UID64" and nested_props == 0:
                    value = bytes.fromhex(take().decode("ascii"))
                    values.append(struct.unpack("<Q", value)[0])
                else:
                    # This request shape is expected to contain only m_UID64.
                    for _ in range(nested_props):
                        take()
        return values
    except (IndexError, ValueError, struct.error):
        return []


def handle_send_mail(handler, target, instance, reqid, comp, session_id,
                     conh, inner_obj, inner_bytes, SERVICE_MAIL_UID, **_kw):
    """Mail.Send (60001) — deliver the composed text mail."""
    receiver_name = _extract_mail_string(inner_bytes, "ReceiverName")
    subject = _extract_mail_string(inner_bytes, "Subject")
    body = _extract_mail_string(inner_bytes, "Body")
    sender = display_name_from_identity(
        handler.user_profile.get("name", "SYSTEM")
        if handler.user_profile else "SYSTEM")
    recipient = db_find_mail_recipient(receiver_name)

    log_req(f">>> Mail.Send: {sender} -> {receiver_name!r}")
    if not recipient:
        resp_inner = _encode_mail_error(-3, "INVALID_RECEIVER")
        _send_response(handler, 60001, resp_inner, comp, session_id,
                       reqid, target, instance, conh, "ServiceMail",
                       SERVICE_MAIL_UID)
        return
    if handler.user_profile and recipient["id"] == handler.user_profile["id"]:
        resp_inner = _encode_mail_error(0, "INVALID_SEND_RECV")
        _send_response(handler, 60001, resp_inner, comp, session_id,
                       reqid, target, instance, conh, "ServiceMail",
                       SERVICE_MAIL_UID)
        return

    db_send_email(recipient["id"], subject, body, sender=sender)
    try:
        notify_new_mail(recipient["id"])
    except Exception as exc:
        log_req(f"    Mail notification push failed: {exc}")
    resp_inner = _encode_mail_send_response()
    _send_response(handler, 60001, resp_inner, comp, session_id,
                   reqid, target, instance, conh, "ServiceMail", SERVICE_MAIL_UID)
    log_req(f"    Mail.Send delivered to {recipient['name']!r}")


def handle_receive(handler, target, instance, reqid, comp, session_id,
                   conh, SERVICE_MAIL_UID, **_kw):
    """Mail.Receive (60002)."""
    if not handler.user_profile:
        db_emails = []
    else:
        db_emails = _db.execute(
            "SELECT id, sender, subject, body, sent_at, gold_delivered, "
            "platinum_delivered, claimed_at FROM emails WHERE user_id=? "
            "ORDER BY id DESC",
            (handler.user_profile["id"],)).fetchall()
    log_req(f">>> Respond Mail.Receive -> {len(db_emails)} emails")
    now = time.strftime("%m/%d/%Y %H:%M:%S", time.gmtime())

    type_names = [
        "Game.Shared.Mail.Messages.Mail+Receive+Response",
        "Game.Shared.Mail.Messages.Mail+PagingResponse",
        "System.Collections.Generic.List`1#Game.Shared.Mail.Messages.Mail+Envelope",
        "Game.Shared.Mail.Messages.Mail+Envelope",
        "Game.Shared.UID", "System.UInt64", "System.String",
        "System.UInt32", "System.Int32", "System.DateTime",
    ]
    def ft(tn):
        if tn not in type_names: type_names.append(tn)
        return type_names.index(tn)

    sizes = []
    buf = io.BytesIO()
    w = lambda s: buf.write(s.encode("utf-8"))
    sep = lambda: buf.write(b";")

    sizes.append(0)
    w(""); sep(); w("0"); sep(); w(str(ft(type_names[0]))); sep(); w("1"); sep()

    f1 = buf.tell(); sizes.append(0)
    w("PagingResp"); sep(); w("1"); sep(); w(str(ft(type_names[1]))); sep(); w("5"); sep()

    f2 = buf.tell(); sizes.append(0)
    w("Envelopes"); sep(); w("2"); sep(); w(str(ft(type_names[2]))); sep(); w("0"); sep()
    w(str(len(db_emails))); sep()

    for i, (eid, sender, subject, body, sent, gold_dlv, plat_dlv, claimed) in enumerate(db_emails):
        fe = buf.tell(); sizes.append(0)
        eidx = len(sizes) - 1
        w(str(i)); sep(); w(str(eidx)); sep(); w(str(ft(type_names[3]))); sep(); w("11"); sep()

        def _wf(ftype, name, val):
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

        def _wuid(name, val):
            f = buf.tell(); sizes.append(0)
            w(name); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("Game.Shared.UID"))); sep(); w("1"); sep()
            fsub = buf.tell(); sizes.append(0)
            w("m_UID64"); sep(); w(str(len(sizes)-1)); sep(); w(str(ft("System.UInt64"))); sep(); w("0"); sep()
            w(hexlify(struct.pack("<Q", val)).decode("ascii")); sep()
            sizes[-1] = buf.tell() - fsub
            sizes[-2] = buf.tell() - f

        _wuid("MailID", _mail_uid64(eid))
        _wuid("SenderID", 0)
        _wf("System.String", "SenderName", sender)
        _wuid("ReceiverID", int(handler.client_reck_id))
        _wf("System.String", "ReceiverName", handler.user_profile["name"])
        _wf("System.String", "Template", "")
        _wf("System.String", "Subject", subject)
        _wf("System.String", "Body", body or "")
        _wf("System.UInt32", "Platinum", plat_dlv or 0)
        _wf("System.UInt32", "Gold", gold_dlv or 0)
        _wf("System.DateTime", "Created", now)
        sizes[eidx] = buf.tell() - fe

    sizes[2] = buf.tell() - f2
    for tname, tval in [("MinTime", now), ("MaxTime", now)]:
        f = buf.tell(); sizes.append(0)
        w(tname); sep(); w(str(len(sizes)-1)); sep(); w(str(ft(type_names[9]))); sep(); w("0"); sep()
        enc = now.encode("utf-8"); w(str(len(enc))); sep(); buf.write(enc)
        sizes[-1] = buf.tell() - f
    for tname, tval in [("Offset", 0), ("Total", len(db_emails))]:
        f = buf.tell(); sizes.append(0)
        w(tname); sep(); w(str(len(sizes)-1)); sep(); w(str(ft(type_names[7]))); sep(); w("0"); sep()
        w(hexlify(struct.pack("<I", tval)).decode("ascii")); sep()
        sizes[-1] = buf.tell() - f
    sizes[1] = buf.tell() - f1
    sizes[0] = buf.tell()
    w(";".join(type_names)); buf.write(b"\n")
    for i, s in enumerate(sizes):
        if i > 0: w(";")
        w(str(s))

    inner = buf.getvalue()
    sz = _send_response(handler, 60002, inner, comp, session_id,
                        reqid, target, instance, conh, "ServiceMail",
                        SERVICE_MAIL_UID)
    log_req(f"    Sent Mail.Receive response ({sz}b)")


def handle_mark_read(handler, target, instance, reqid, comp, session_id,
                     conh, SERVICE_MAIL_UID, **_kw):
    """Mail.Delivered (60005) — return the current user's sent mail."""
    sender = display_name_from_identity(
        handler.user_profile.get("name", "SYSTEM")
        if handler.user_profile else "SYSTEM")
    db_emails = db_get_sent_mail_list(sender) if handler.user_profile else []
    log_req(f">>> Respond Mail.Delivered -> {len(db_emails)} emails")
    now = time.strftime("%m/%d/%Y %H:%M:%S", time.gmtime())
    type_names = [
        "Game.Shared.Mail.Messages.Mail+Delivered+Response",
        "Game.Shared.Mail.Messages.Mail+PagingResponse",
        "System.Collections.Generic.List`1#Game.Shared.Mail.Messages.Mail+Envelope",
        "System.DateTime", "System.UInt32",
    ]
    def ft(tn):
        if tn not in type_names: type_names.append(tn)
        return type_names.index(tn)
    sizes = []
    buf = io.BytesIO()
    w = lambda s: buf.write(s.encode("utf-8"))
    sep = lambda: buf.write(b";")
    sizes.append(0)
    w(""); sep(); w("0"); sep(); w(str(ft(type_names[0]))); sep(); w("1"); sep()
    f1 = buf.tell(); sizes.append(0)
    w("PagingResp"); sep(); w("1"); sep(); w(str(ft(type_names[1]))); sep(); w("5"); sep()
    f2 = buf.tell(); sizes.append(0)
    w("Envelopes"); sep(); w("2"); sep(); w(str(ft(type_names[2]))); sep(); w("0"); sep()
    w(str(len(db_emails))); sep()

    for i, (eid, sender_name, receiver_name, subject, body, sent,
            gold_dlv, plat_dlv, claimed) in enumerate(db_emails):
        fe = buf.tell(); sizes.append(0)
        eidx = len(sizes) - 1
        w(str(i)); sep(); w(str(eidx)); sep(); w(str(ft(type_names[2].split("#", 1)[1]))); sep(); w("11"); sep()

        def _wf(ftype, name, val):
            f = buf.tell(); sizes.append(0)
            w(name); sep(); w(str(len(sizes) - 1)); sep(); w(str(ft(ftype))); sep(); w("0"); sep()
            if ftype == "System.UInt64":
                w(hexlify(struct.pack("<Q", val)).decode("ascii")); sep()
            elif ftype == "System.String":
                enc = (val or "").encode("utf-8")
                w(str(len(enc))); sep(); buf.write(enc)
            elif ftype == "System.UInt32":
                w(hexlify(struct.pack("<I", val or 0)).decode("ascii")); sep()
            elif ftype == "System.DateTime":
                enc = str(val).encode("utf-8")
                w(str(len(enc))); sep(); buf.write(enc)
            sizes[-1] = buf.tell() - f

        def _wuid(name, val):
            f = buf.tell(); sizes.append(0)
            w(name); sep(); w(str(len(sizes) - 1)); sep(); w(str(ft("Game.Shared.UID"))); sep(); w("1"); sep()
            fsub = buf.tell(); sizes.append(0)
            w("m_UID64"); sep(); w(str(len(sizes) - 1)); sep(); w(str(ft("System.UInt64"))); sep(); w("0"); sep()
            w(hexlify(struct.pack("<Q", val)).decode("ascii")); sep()
            sizes[-1] = buf.tell() - fsub
            sizes[-2] = buf.tell() - f

        _wuid("MailID", _mail_uid64(eid))
        _wuid("SenderID", 0)
        _wf("System.String", "SenderName", sender_name)
        _wuid("ReceiverID", 0)
        _wf("System.String", "ReceiverName", display_name_from_identity(receiver_name or ""))
        _wf("System.String", "Template", "")
        _wf("System.String", "Subject", subject)
        _wf("System.String", "Body", body)
        _wf("System.UInt32", "Platinum", plat_dlv)
        _wf("System.UInt32", "Gold", gold_dlv)
        _wf("System.DateTime", "Created", now)
        sizes[eidx] = buf.tell() - fe

    sizes[2] = buf.tell() - f2
    for tname in ("MinTime", "MaxTime"):
        f = buf.tell(); sizes.append(0)
        field_idx = len(sizes) - 1
        w(tname); sep(); w(str(field_idx)); sep(); w(str(ft(type_names[3]))); sep(); w("0"); sep()
        enc = now.encode("utf-8"); w(str(len(enc))); sep(); buf.write(enc)
        sizes[field_idx] = buf.tell() - f
    for tname, value in (("Offset", 0), ("Total", len(db_emails))):
        f = buf.tell(); sizes.append(0)
        field_idx = len(sizes) - 1
        w(tname); sep(); w(str(field_idx)); sep(); w(str(ft(type_names[4]))); sep(); w("0"); sep()
        w(hexlify(struct.pack("<I", value)).decode("ascii")); sep()
        sizes[field_idx] = buf.tell() - f
    sizes[1] = buf.tell() - f1
    sizes[0] = buf.tell()
    w(";".join(type_names)); buf.write(b"\n")
    for i, s in enumerate(sizes):
        if i > 0:
            w(";")
        w(str(s))
    inner = buf.getvalue()
    sz = _send_response(handler, 60005, inner, comp, session_id,
                        reqid, target, instance, conh, "ServiceMail",
                        SERVICE_MAIL_UID)
    log_req(f"    Sent Mail.Delivered response ({sz}b)")


def handle_mark_sent_delete(handler, target, instance, reqid, comp, session_id,
                            conh, inner_obj, inner_bytes, SERVICE_MAIL_UID,
                            **_kw):
    """Mail.MarkSentMailDelete (60008) — delete selected sent mail."""
    sender = display_name_from_identity(
        handler.user_profile.get("name", "SYSTEM")
        if handler.user_profile else "SYSTEM")
    uid_values = _extract_mail_uid_list(inner_bytes)
    email_ids = [_mail_id_from_uid64(uid64) for uid64 in uid_values]
    updated = db_delete_sent_mail(sender, email_ids) if handler.user_profile else 0
    log_req(f">>> Mail.MarkSentMailDelete: {len(email_ids)} requested, {updated} deleted")

    inner = encode_objfmt_response(
        ["Game.Shared.Mail.Messages.Mail+MarkSentMailDelete+Response"], [])
    sz = _send_response(handler, 60008, inner, comp, session_id,
                        reqid, target, instance, conh, "ServiceMail",
                        SERVICE_MAIL_UID)
    log_req(f"    Sent Mail.MarkSentMailDelete response ({sz}b)")


def handle_send(handler, target, instance, reqid, comp, session_id,
                conh, SERVICE_MAIL_UID, **_kw):
    """Mail.MarkRead (60004)."""
    log_req(">>> Respond Mail.MarkRead")
    if handler.user_profile:
        handler._application.execute(MarkMailReadCommand(
            user_id=handler.user_profile["id"]))
    inner = encode_objfmt_response(
        ["Game.Shared.Mail.Messages.Mail+MarkRead+Response"], [])
    sz = _send_response(handler, 60004, inner, comp, session_id,
                        reqid, target, instance, conh, "ServiceMail",
                        SERVICE_MAIL_UID)
    log_req(f"    Sent Mail.MarkRead response ({sz}b)")


def handle_claim(handler, target, instance, reqid, comp, session_id,
                 conh, SERVICE_MAIL_UID, **_kw):
    """Mail.MarkDelete (60006)."""
    log_req(">>> Respond Mail.MarkDelete")
    if handler.user_profile:
        handler._application.execute(DeleteMailCommand(
            user_id=handler.user_profile["id"]))
    inner = encode_objfmt_response(
        ["Game.Shared.Mail.Messages.Mail+MarkDelete+Response"], [])
    sz = _send_response(handler, 60006, inner, comp, session_id,
                        reqid, target, instance, conh, "ServiceMail",
                        SERVICE_MAIL_UID)
    log_req(f"    Sent Mail.MarkDelete response ({sz}b)")


def handle_delete(handler, target, instance, reqid, comp, session_id,
                  conh, inner_obj, SERVICE_MAIL_UID, **_kw):
    """Mail.Claim (60003)."""
    log_req(">>> Mail.Claim (dt=60003)")
    p = handler.user_profile
    mail_id_obj = inner_obj.get("MailID", {})
    mail_id_64 = 0
    if isinstance(mail_id_obj, dict):
        mail_id_64 = mail_id_obj.get("m_UID64", 0)
    elif isinstance(mail_id_obj, int):
        mail_id_64 = mail_id_obj
    eid = _mail_id_from_uid64(mail_id_64)

    gold_granted = plat_granted = 0
    if eid > 0 and p:
        claim = handler._application.execute(ClaimMailCommand(
            user_id=p["id"], email_id=eid)).value
        gold_granted = claim["gold"]
        plat_granted = claim["platinum"]
        if gold_granted or plat_granted:
            p["gold"] += gold_granted
            p["platinum"] += plat_granted
            log_req(f"    Claimed mail #{eid}: gold+{gold_granted} plat+{plat_granted}")

    inner = encode_objfmt_response(
        ["Game.Shared.Mail.Messages.Mail+Claim+Response",
         "System.UInt64", "System.UInt32",
         "System.Collections.Generic.List`1#Game.Shared.Mail.Messages.Mail+AttachmentCard",
         "System.Collections.Generic.List`1#Game.Shared.Mail.Messages.Mail+AttachmentInventory"],
        [("EnvCLID", "ulong", mail_id_64),
         ("CardC", "uint", 0), ("InvenC", "uint", 0),
         ("Cards", "coll",
          ("System.Collections.Generic.List`1#Game.Shared.Mail.Messages.Mail+AttachmentCard", 0)),
         ("Inven", "coll",
          ("System.Collections.Generic.List`1#Game.Shared.Mail.Messages.Mail+AttachmentInventory", 0))]
    )
    sz = _send_response(handler, 60003, inner, comp, session_id,
                        reqid, target, instance, conh, "ServiceMail",
                        SERVICE_MAIL_UID)
    log_req(f"    Sent Mail.Claim response ({sz}b)")
