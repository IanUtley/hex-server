"""Focused regression tests for the mail service protocol responses."""

import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_delivered_response_has_a_parseable_size_table():
    import services.mail as mail_service

    class Handler:
        client_uid = 1
        sid = "sid"
        scnt = 0
        user_profile = None

    captured = {}

    def capture_response(handler, data_type, inner, *args, **kwargs):
        captured["inner"] = inner
        return len(inner)

    with mock.patch.object(mail_service, "_send_response",
                           side_effect=capture_response), \
            mock.patch.object(mail_service, "log_req"):
        mail_service.handle_mark_read(
            Handler(), "ServiceMail", "Shared", 2, 0, "session", 0, "mail")

    body, size_table = captured["inner"].rsplit(b"\n", 1)
    sizes = [int(value) for value in size_table.split(b";")]

    assert sizes[0] == body.index(
        b"Game.Shared.Mail.Messages.Mail+Delivered+Response")
    assert sizes[0] > 0
    assert len(sizes) == 7


def test_send_mail_delivers_to_display_name_and_replies():
    import services.mail as mail_service

    class Handler:
        client_uid = 1
        sid = "sid"
        scnt = 0
        user_profile = {"id": 7, "name": "God#0552"}

    raw = (b";ReceiverName;9;5;0;5;DevilSubject;10;5;0;7;Subject"
           b"Body;11;5;0;14;A message here;")
    sent = {}

    def capture_response(handler, data_type, inner, *args, **kwargs):
        sent["data_type"] = data_type
        sent["inner"] = inner
        return len(inner)

    with mock.patch.object(
            mail_service, "db_find_mail_recipient",
            return_value={"id": 42, "name": "Devil#5805"}), \
            mock.patch.object(mail_service, "db_send_email") as send_email, \
            mock.patch.object(mail_service, "_send_response",
                              side_effect=capture_response), \
            mock.patch.object(mail_service, "log_req"):
        mail_service.handle_send_mail(
            Handler(), "ServiceMail", "Shared", 2, 0, "session", 0,
            {}, raw, "mail")

    send_email.assert_called_once_with(
        42, "Subject", "A message here", sender="God")
    assert sent["data_type"] == 60001
    assert b"Game.Shared.Mail.Messages.Mail+Send+Response" in sent["inner"]


def test_delivered_response_includes_sent_mail():
    import services.mail as mail_service

    class Handler:
        client_uid = 1
        sid = "sid"
        scnt = 0
        user_profile = {"id": 7, "name": "God#0552"}

    sent = {}

    def capture_response(handler, data_type, inner, *args, **kwargs):
        sent["inner"] = inner
        return len(inner)

    rows = [(1, "God", "Devil#5805", "Subject", "Body",
             "2026-08-26 16:08:24", 0, 0, None)]
    with mock.patch.object(mail_service, "db_get_sent_mail_list",
                           return_value=rows), \
            mock.patch.object(mail_service, "_send_response",
                              side_effect=capture_response), \
            mock.patch.object(mail_service, "log_req"):
        mail_service.handle_mark_read(
            Handler(), "ServiceMail", "Shared", 2, 0, "session", 0, "mail")

    body, size_table = sent["inner"].rsplit(b"\n", 1)
    sizes = [int(value) for value in size_table.split(b";")]
    assert sizes[0] == body.index(
        b"Game.Shared.Mail.Messages.Mail+Delivered+Response")
    assert b"Devil" in body
    assert b"Subject" in body
    assert b"Body" in body
    assert b"Created;17;3;0;19;" in body


def test_mark_sent_delete_parses_mail_uid_and_deletes_selected_message():
    import services.mail as mail_service

    class Handler:
        client_uid = 1
        sid = "sid"
        scnt = 0
        user_profile = {"id": 7, "name": "God#0552"}

    # ObjFmt request shape captured from the client: OwnerID followed by one
    # Mail UID in IDs.  The UID encodes database email id 1 as UID.Type.Mail.
    raw = (b";0;0;2;OwnerID;1;1;1;m_UID64;2;2;0;"
           b"F4904FB3351F3D606;IDs;3;3;0;1;0;4;1;1;"
           b"m_UID64;5;2;0;09E9030000000000;")
    captured = {}

    def capture_response(handler, data_type, inner, *args, **kwargs):
        captured["data_type"] = data_type
        captured["inner"] = inner
        return len(inner)

    with mock.patch.object(mail_service, "db_delete_sent_mail",
                           return_value=1) as delete_mail, \
            mock.patch.object(mail_service, "_send_response",
                              side_effect=capture_response), \
            mock.patch.object(mail_service, "log_req"):
        mail_service.handle_mark_sent_delete(
            Handler(), "ServiceMail", "Shared", 2, 0, "session", 0,
            {}, raw, "mail")

    delete_mail.assert_called_once_with("God", [1])
    assert captured["data_type"] == 60008
    assert b"Game.Shared.Mail.Messages.Mail+MarkSentMailDelete+Response" in captured["inner"]


if __name__ == "__main__":
    test_delivered_response_has_a_parseable_size_table()
    test_send_mail_delivers_to_display_name_and_replies()
    test_delivered_response_includes_sent_mail()
    test_mark_sent_delete_parses_mail_uid_and_deletes_selected_message()
    print("Mail protocol tests passed")
