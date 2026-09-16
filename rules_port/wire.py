"""Normalize existing HConnect transaction classifications into rules intents.

The protocol decoder remains responsible for ObjFmt bytes and typed nested
payloads. This module only maps the already-classified command to a
``RulesTransaction``; it never mutates a session or infers state from card
text. Payload-bearing transaction types can be supplied by the decoder once
their typed fields are available.
"""

from __future__ import annotations

import re
import struct
from dataclasses import replace
from typing import Any, Mapping

import game_engine

from .session import RulesTransaction


_SESSION_CARD_UID = re.compile(rb"m_UID64")


def extract_session_card_uids(raw: bytes, *, exclude=()) -> tuple[int, ...]:
    """Decode explicitly serialized ``SessionCardId`` values in wire order.

    Client builds differ in whether a nested UID has a ``value`` wrapper.
    Walk only the small scalar field following each explicit ``m_UID64``
    label, then retain the existing card-type validation.
    """
    if isinstance(raw, memoryview):
        raw = raw.tobytes()
    elif isinstance(raw, bytearray):
        raw = bytes(raw)
    if not isinstance(raw, bytes):
        return ()
    excluded = {int(value) for value in exclude}
    values = []
    for match in _SESSION_CARD_UID.finditer(raw):
        fields = raw[match.end():].split(b";", 9)
        for field in fields[1:]:
            if len(field) != 16 or not re.fullmatch(rb"[0-9A-Fa-f]{16}", field):
                continue
            try:
                value = struct.unpack("<Q", bytes.fromhex(
                    field.decode("ascii")))[0]
            except (TypeError, ValueError, UnicodeDecodeError, struct.error):
                break
            if (value & 0xFF) == 1 and value not in excluded:
                values.append(int(value))
            break
    return tuple(values)


def _coerce_phase(value):
    if isinstance(value, str):
        return getattr(game_engine.ETurnPhases, value,
                       game_engine.ETurnPhases.Unknown)
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return game_engine.ETurnPhases.Unknown
    names = (name for name in vars(game_engine.ETurnPhases)
             if not name.startswith("_") and isinstance(getattr(
                 game_engine.ETurnPhases, name), int))
    for name in names:
        if getattr(game_engine.ETurnPhases, name) == numeric:
            return getattr(game_engine.ETurnPhases, name)
    return game_engine.ETurnPhases.Unknown




def normalize_player_transaction(command, player_id, *, current_phase=None,
                                 payload: Mapping[str, Any] | None = None):
    """Return one normalized transaction or ``None`` when payload decoding is incomplete."""
    if payload is None:
        payload = getattr(command, "typed_payload", None)
    phase = _coerce_phase(
        current_phase if current_phase is not None else
        getattr(command, "pass_turn_phase", None))
    raw = getattr(command, "inner_bytes", b"")

    if getattr(command, "is_pass_priority", False):
        return RulesTransaction.pass_priority(player_id, phase)
    if getattr(command, "is_choose_pick", False):
        if b"ChooseDrawTransaction" in raw:
            return RulesTransaction.choose_draw_first(player_id, phase)
        if b"ChoosePlayTransaction" in raw:
            return RulesTransaction.choose_play_first(player_id, phase)
        return None
    if getattr(command, "is_mulligan_keep", False):
        return RulesTransaction.accept_starting_hand(player_id)
    if getattr(command, "is_mulligan_redraw", False):
        return RulesTransaction.mulligan(player_id)
    if getattr(command, "is_priority_sync", False):
        return RulesTransaction.request_priority_sync(player_id)
    if getattr(command, "is_cancel_auto_pass", False):
        return RulesTransaction.cancel_auto_pass(player_id)
    if getattr(command, "is_discard", False):
        if payload is None or payload.get("card_id") is None:
            return None
        return RulesTransaction.discard(player_id, payload["card_id"])
    if getattr(command, "is_ready_card", False):
        if payload is None or payload.get("card_ids") is None:
            return None
        return RulesTransaction.ready_card(player_id, payload["card_ids"])
    if getattr(command, "is_quit_game", False):
        payload = payload or {}
        return RulesTransaction.quit_game(
            player_id, payload.get("was_bugged", False),
            payload.get("quit_entire_series", False))
    # The application classifier preserves the client's semantic name
    # (``is_set_stops``); accept the longer adapter spelling as well for
    # callers that construct commands directly.
    if (getattr(command, "is_set_stops", False) or
            getattr(command, "is_set_turn_phases", False)):
        if payload is None:
            return None
        return RulesTransaction.set_turn_phases(
            player_id, payload.get("self_phases", ()),
            payload.get("opponent_phases", ()))
    if getattr(command, "is_request_player_options", False):
        return RulesTransaction.request_player_options(player_id)
    if getattr(command, "is_state_checksum", False):
        if payload is None or payload.get("checksum_data") is None:
            return None
        return RulesTransaction.send_state_checksum(player_id, payload["checksum_data"])
    if getattr(command, "is_tip_window_closed", False):
        return RulesTransaction.tip_window_closed(player_id)
    if getattr(command, "is_encounter_mod_dialog", False):
        if payload is None or payload.get("conversation_id") is None:
            return None
        return RulesTransaction.encounter_mod_dialog(
            player_id, payload["conversation_id"])
    if getattr(command, "is_set_auto_pass", False):
        if payload is None:
            return None
        return RulesTransaction.set_auto_pass(
            player_id, payload.get("as_active", False),
            payload.get("passing_state"))

    # These classes contain nested SessionCardId/AbilityActivationData values;
    # do not guess them from raw bytes. A typed decoder passes the normalized
    # payload explicitly and receives the same C# requirement composition.
    if payload is None:
        return None
    # Continuation checkpoints are explicit RulesPort intents.  The host may
    # annotate a decoded ObjFmt response before normalization; keeping these
    # branches ahead of fresh-activation decoding prevents a class-23 or
    # triggered response from being interpreted as a new ability activation.
    if payload.get("_discard_continuation"):
        return RulesTransaction.resolve_discard_continuation(
            player_id, payload.get("activation_data", payload))
    if payload.get("_triggered_continuation"):
        return RulesTransaction.resolve_triggered_continuation(
            player_id, payload.get("activation_data", payload))
    card_id = payload.get("card_id")
    if getattr(command, "is_play_resource", False):
        return (None if card_id is None else
                RulesTransaction.play_resource(player_id, card_id))
    play_kind = next((kind for kind in ("troop", "artifact", "spell",
                                        "champion")
                      if getattr(command, f"is_play_{kind}", False)), None)
    if play_kind is not None:
        if card_id is None:
            return None
        return RulesTransaction.play_card(
            f"play_{play_kind}", player_id, card_id,
            payload.get("ability_data", ()),
            payload.get("playing_for_free", False), phase)
    if (getattr(command, "is_ability_activate", False) and
            not getattr(command, "is_activate_triggered_abilities", False)):
        # ActivateAbilityTransaction carries nested card/ability identifiers
        # and activation choices.  Those values must come from the typed
        # decoder; never attempt to recover them from ObjFmt bytes here.
        source_card_id = payload.get("source_card_id")
        ability_template_id = payload.get("ability_template_id")
        activation_data = payload.get("activation_data")
        if (source_card_id is None or not ability_template_id or
                activation_data is None):
            return None
        return RulesTransaction.activate_ability(
            player_id, source_card_id, ability_template_id, activation_data,
            payload.get("ability_instance_id", 0))
    if getattr(command, "is_set_ability_data", False):
        if payload.get("_choice_continuation"):
            return RulesTransaction.resolve_choice_continuation(
                player_id, payload.get("activation_data", {}))
        # A class-23 response from Mono may omit AbilityInstanceId. The live
        # dispatcher can fill it from the pending continuation; never pass
        # None into the typed transaction constructor.
        if (payload.get("ability_instance_id") is None or
                payload.get("ability_instance_id") == ""):
            return None
        return RulesTransaction.set_ability_activation_data(
            player_id, payload.get("ability_instance_id"),
            payload.get("activation_data", {}))
    if getattr(command, "is_activate_triggered_abilities", False):
        if payload is None or payload.get("activation_data") is None:
            return None
        activation_data = payload["activation_data"]
        # Mono serializes a single triggered selection as one
        # AbilityActivationData mapping, while the transaction contract is a
        # List<AbilityActivationData>.  Passing the mapping directly makes
        # ``tuple(mapping)`` yield field names (SourceCardId, TargetMap, ...),
        # which then fail AbilitiesAreTriggered/XCost validation.
        if isinstance(activation_data, Mapping):
            activation_data = (activation_data,)
        return RulesTransaction.activate_triggered_abilities(
            player_id, activation_data)
    if getattr(command, "is_assign_damage", False):
        return RulesTransaction.assign_damage_order(
            player_id, phase, payload.get("assignments", ()))
    if getattr(command, "is_commit_attack", False):
        return RulesTransaction.commit_troops_to_attack(
            player_id, phase, payload.get("declarations", ()))
    if getattr(command, "is_commit_defense", False):
        return RulesTransaction.commit_troops_to_defense(
            player_id, phase, payload.get("declarations", ()))
    return None


def submit_classified_transaction(session, command, player_id, *,
                                  payload: Mapping[str, Any] | None = None) -> bool:
    """Bridge an existing HConnect classification into the port queue.

    Protocol decoding and legacy handlers remain outside this function.  It
    only normalizes the already-classified command against the port's current
    phase and submits the resulting intent, making migration a selectable
    ingress seam rather than a second parser or a second persistence path.
    """
    # A triggered ability's client reply carries the same
    # ``m_AbilityActivationData`` envelope as a manual activation.  Once the
    # RulesPort scheduler has a pending activation, that envelope is a
    # continuation and must use the SetAbilityActivationData contract.
    pending = getattr(session, "pending_activation", None)
    if (getattr(command, "is_ability_activate", False) and
            isinstance(pending, Mapping)):
        command = replace(command, is_ability_activate=False,
                          is_set_ability_data=True)
    coerce_player = getattr(session, "coerce_transaction_player_id", None)
    if callable(coerce_player):
        player_id = coerce_player(player_id)
    transaction = normalize_player_transaction(
        command, player_id, current_phase=session.current_turn_phase,
        payload=payload)
    return bool(transaction is not None and session.submit_transaction(transaction))
