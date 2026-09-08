"""Transactional application-command dispatcher.

The protocol layer should decode a request into a command and call
``execute``. Command handlers receive the active transaction connection and
must not commit or publish events themselves. Events are published only
after the transaction context exits successfully.
"""

from collections.abc import Callable

import db
import game_session
import profile_db

from .commands import (JoinSessionCommand, RemoveSessionCommand,
                       ClaimMailCommand, DeleteMailCommand,
                       MarkMailReadCommand, SetSessionStateCommand,
                       PurchaseStoreItemCommand, RedeemCodeCommand,
                       SocialMutationCommand,
                       StartEncounterCommand, StartSessionCommand)
from .results import (CommandResult, MailChanged, SessionRemoved,
                      SessionStateChanged, SocialChanged, StoreChanged)


class ApplicationCommandDispatcher:
    """Execute application commands inside explicit transaction boundaries."""

    def __init__(self, event_publisher: Callable[[tuple], None] | None = None,
                 database_path=None):
        self._event_publisher = event_publisher or (lambda events: None)
        self._database_path = database_path
        self._handlers = {
            RemoveSessionCommand: self._remove_session,
            StartSessionCommand: self._start_session,
            StartEncounterCommand: self._start_encounter,
            JoinSessionCommand: self._join_session,
            SetSessionStateCommand: self._set_session_state,
            MarkMailReadCommand: self._mark_mail_read,
            DeleteMailCommand: self._delete_mail,
            ClaimMailCommand: self._claim_mail,
            PurchaseStoreItemCommand: self._purchase_store_item,
            RedeemCodeCommand: self._redeem_code,
            SocialMutationCommand: self._social_mutation,
        }

    def execute(self, command):
        """Execute ``command``, then publish its events after commit."""
        try:
            handler = self._handlers[type(command)]
        except KeyError as exc:
            raise TypeError(
                f"Unsupported application command: {type(command).__name__}") from exc

        with db.transaction(self._database_path) as tx:
            result = handler(tx, command)
            if not isinstance(result, CommandResult):
                raise TypeError(
                    f"Application handler returned {type(result).__name__}; "
                    "expected CommandResult")

        # The transaction has committed at this point. A publisher failure
        # cannot roll back the already-authoritative database state.
        if result.events:
            self._event_publisher(result.events)
        return result

    @staticmethod
    def dispatch_request(command, legacy_handler):
        """Dispatch an incoming protocol request through the app boundary.

        This adapter keeps the wire protocol stable while the existing
        per-service branches are migrated to typed commands. Migrated
        state-changing commands use ``execute`` above; legacy handlers must
        not be treated as transactional until they accept the command
        connection explicitly.
        """
        if not callable(legacy_handler):
            raise TypeError("legacy_handler must be callable")
        return legacy_handler(command)

    @staticmethod
    def dispatch_player_transaction(command, handler):
        """Route a classified PlayerTransaction to its command handler."""
        if not callable(handler):
            raise TypeError("player transaction handler must be callable")
        return handler(command)

    @staticmethod
    def _remove_session(tx, command):
        session = game_session.find_session_by_player(command.player_uid, conn=tx)
        if session is None:
            return CommandResult()

        game_session.remove_session(session.session_name, conn=tx)
        return CommandResult(
            value=session.session_name,
            events=(SessionRemoved(
                session_name=session.session_name,
                player_uid=command.player_uid,
                reason=command.reason,
            ),),
        )

    @staticmethod
    def _start_session(tx, command):
        session = game_session.create_encounter_session(
            command.session_name, {}, command.player_uid, conn=tx)
        session.set_state("created", conn=tx)
        return CommandResult(value=session)

    @staticmethod
    def _start_encounter(tx, command):
        session = game_session.create_encounter_session(
            command.session_name, command.encounter_data,
            command.player_uid, conn=tx)
        session.set_state("created", conn=tx)
        return CommandResult(value=session)

    @staticmethod
    def _join_session(tx, command):
        session = game_session.find_session_by_id(command.session_id, conn=tx)
        if session is None:
            return CommandResult()
        session.add_player(command.player_uid, 0, conn=tx)
        session.set_state("joined", conn=tx)
        return CommandResult(value=session)

    @staticmethod
    def _set_session_state(tx, command):
        session = game_session.find_session_by_player(command.player_uid, conn=tx)
        if session is None:
            return CommandResult()
        session.set_state(command.state, conn=tx)
        return CommandResult(
            value=session,
            events=(SessionStateChanged(
                session_name=session.session_name,
                player_uid=command.player_uid,
                state=command.state,
            ),),
        )

    @staticmethod
    def _mark_mail_read(tx, command):
        profile_db.db_mark_mail_read(command.user_id, conn=tx)
        return CommandResult(events=(MailChanged(
            user_id=command.user_id, operation="mark_read"),))

    @staticmethod
    def _delete_mail(tx, command):
        profile_db.db_delete_mail(command.user_id, conn=tx)
        return CommandResult(events=(MailChanged(
            user_id=command.user_id, operation="delete"),))

    @staticmethod
    def _claim_mail(tx, command):
        value = profile_db.db_claim_mail_for_user(
            command.user_id, command.email_id, conn=tx)
        if value is None:
            return CommandResult(value={"gold": 0, "platinum": 0})
        return CommandResult(
            value=value,
            events=(MailChanged(
                user_id=command.user_id,
                operation="claim",
                email_id=command.email_id,
            ),),
        )

    @staticmethod
    def _purchase_store_item(tx, command):
        from services.store import apply_purchase
        value = apply_purchase(
            tx, command.user_id, command.item_id, command.quantity)
        return CommandResult(value=value, events=(StoreChanged(
            user_id=command.user_id,
            operation="purchase",
            item_id=command.item_id,
        ),))

    @staticmethod
    def _redeem_code(tx, command):
        from services.store import apply_redeem
        value = apply_redeem(tx, command.user_id, command.code)
        return CommandResult(value=value, events=(StoreChanged(
            user_id=command.user_id,
            operation="redeem" if value["redeemed"] else "invalid",
        ),))

    @staticmethod
    def _social_mutation(tx, command):
        import db
        operations = {
            "add_friend": db.db_send_friend_request,
            "accept_friend": db.db_accept_friend_request,
            "ignore_friend_request": db.db_ignore_friend_request,
            "remove_friend": db.db_remove_friend,
            "ignore_player": db.db_ignore_player,
            "unignore_player": db.db_unignore_player,
        }
        try:
            operation = operations[command.operation]
        except KeyError as exc:
            raise ValueError(
                f"Unknown social operation: {command.operation}") from exc
        value = operation(
            command.user_id, command.target_name, conn=tx)
        target_user_id = value[1] if len(value) > 1 else None
        if command.operation == "add_friend":
            target_user_id = value[2]
        return CommandResult(value=value, events=(SocialChanged(
            user_id=command.user_id,
            operation=command.operation,
            target_user_id=target_user_id,
        ),))
