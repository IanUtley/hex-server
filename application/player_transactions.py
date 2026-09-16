"""Classification of GameSession PlayerTransaction payloads."""

from dataclasses import dataclass
import re
import struct
from typing import Mapping, Any


def _transaction_fields(inner_bytes):
    fields = {}
    if not isinstance(inner_bytes, bytes):
        return fields
    try:
        for key in (b"m_TransactionId", b"m_QuitEntireSeries",
                    b"m_WasBugged", b"m_Conceeded", b"m_Surrendered"):
            pos = inner_bytes.find(key)
            if pos < 0:
                continue
            rest = inner_bytes[pos + len(key):]
            parts = rest.split(b";", 5)
            if len(parts) >= 5:
                fields[key.decode()] = parts[4].decode("ascii")
    except (UnicodeDecodeError, ValueError, TypeError):
        return fields
    return fields


def _extract_enum_int(inner_bytes, field):
    if not isinstance(inner_bytes, bytes):
        return None
    idx = inner_bytes.find(field.encode())
    if idx < 0:
        return None
    value_idx = inner_bytes.find(b"value__", idx)
    if value_idx < 0:
        return None
    parts = inner_bytes[value_idx + len(b"value__") + 1:].split(b";", 6)
    if len(parts) < 4:
        return None
    try:
        return int.from_bytes(bytes.fromhex(parts[3].decode("ascii")), "little")
    except (ValueError, UnicodeDecodeError):
        return None


def extract_resource_guid(inner_bytes, field):
    """Extract a GUID-backed ResourceId field from an ObjFmt transaction.

    The generic ObjFmt decoder intentionally skips nested ResourceId values,
    but conversation acknowledgements carry the only authoritative value in
    ``ConversationId``.  Keep this parser narrowly scoped to a named field;
    it does not infer rules from display text or from arbitrary GUIDs.
    """
    if not isinstance(inner_bytes, bytes):
        return None
    start = inner_bytes.find(str(field).encode())
    if start < 0:
        return None
    match = re.search(
        rb"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        rb"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        inner_bytes[start:start + 512],
    )
    return match.group(0).decode().lower() if match else None


def extract_ability_guid(inner_bytes):
    """Extract an activation's AbilityTemplateId without counting fields."""
    return extract_resource_guid(inner_bytes, "AbilityTemplateId")


def _normalize_activation_data(value):
    """Normalize decoded C# AbilityActivationData field names.

    ObjFmt preserves the C# member casing while the RulesPort activation model
    uses snake-case keys.  Keep this conversion restricted to the named
    activation envelope; it must not infer targets from arbitrary display
    strings or unrelated UIDs.
    """
    if isinstance(value, (list, tuple)):
        return tuple(_normalize_activation_data(item) for item in value)
    if not isinstance(value, Mapping):
        return value
    aliases = {
        "abilityinstanceid": "ability_instance_id",
        "abilitytemplateid": "ability_template_id",
        "sourcecardid": "source_card_id",
        "targetmap": "target_map",
        "optionmap": "option_map",
        "variables": "variables",
        "xcostdata": "x_cost_data",
        "xcost": "x_cost",
        "disable": "disable",
        "optedin": "opted",
        "optedinset": "opted_in_set",
        "index": "index",
        "muid64": "uid64",
        "msessioncardids": "session_card_ids",
        "cardstosacrifice": "cards_to_sacrifice",
        "mplayerids": "player_ids",
        "resourcexcost": "resource_x_cost",
        "chargepointsxcost": "charge_points_x_cost",
        "lifexcost": "life_x_cost",
        "counterxcost": "counter_x_cost",
        "spellpointsxcost": "spell_points_x_cost",
        "setvalues": "set_values",
    }
    result = {}
    for key, item in value.items():
        norm = aliases.get(str(key).replace("_", "").lower(), key)
        result[norm] = _normalize_activation_data(item)
    if "x_cost" not in result and isinstance(result.get("x_cost_data"), Mapping):
        nested = result["x_cost_data"]
        for key in ("resource_x_cost", "x_cost", "value", "amount"):
            if key in nested:
                result["x_cost"] = nested[key]
                break
    target_map = result.get("target_map")
    if isinstance(target_map, Mapping):
        normalized_targets = {}
        for index, target in target_map.items():
            try:
                index = int(index)
            except (TypeError, ValueError):
                continue
            if isinstance(target, Mapping):
                target = target.get("session_card_ids") or target.get("player_ids")
            if target is None:
                continue
            if not isinstance(target, (list, tuple, set)):
                target = (target,)
            values = []
            for selected in target:
                if isinstance(selected, Mapping):
                    selected = selected.get("uid64", selected.get("UID", selected))
                    if isinstance(selected, Mapping):
                        selected = selected.get("value", selected)
                    if isinstance(selected, Mapping):
                        selected = selected.get("uid64", selected.get("m_UID64", selected))
                try:
                    values.append(int(selected))
                except (TypeError, ValueError):
                    continue
            normalized_targets[index] = values
        result["target_map"] = normalized_targets
    # XCostData carries additional card costs outside TargetMap.  Preserve
    # the explicit label so abilities with both a sacrifice cost and an
    # effect target (Bunoshi is the canonical shape) cannot reuse the cost
    # card as the effect target.
    sacrifice = result.get("cards_to_sacrifice")
    if sacrifice is None and isinstance(result.get("x_cost_data"), Mapping):
        sacrifice = result["x_cost_data"].get("cards_to_sacrifice")
    if sacrifice is not None and "cost_target_map" not in result:
        if not isinstance(sacrifice, (list, tuple, set)):
            sacrifice = (sacrifice,)
        values = []
        for selected in sacrifice:
            if isinstance(selected, Mapping):
                selected = selected.get("uid64", selected.get("UID", selected))
                if isinstance(selected, Mapping):
                    selected = selected.get("value", selected)
                if isinstance(selected, Mapping):
                    selected = selected.get("uid64", selected.get("m_UID64", selected))
            try:
                values.append(int(selected))
            except (TypeError, ValueError):
                continue
        if values:
            result["cost_target_map"] = {0: values}
    return result


def _normalize_checksum_data(value):
    """Normalize the named C# ``SessionChecksumData`` record.

    Checksums are a legacy diagnostic transaction (the client session treats
    them as defunct), but they still need to cross the typed RulesPort ingress
    so they cannot accidentally execute through the legacy dispatcher.
    """
    if not isinstance(value, Mapping):
        return value
    aliases = {
        "sessionchecksum": "session_checksum",
        "wantdata": "want_data",
        "playerchecksums": "player_checksums",
    }
    return {
        aliases.get(str(key).replace("_", "").lower(), key): item
        for key, item in value.items()
    }


def typed_payload_from_decoded(command, decoded):
    """Extract only named, decoder-owned fields for rules-port ingress.

    This deliberately does not scan arbitrary identifiers: nested values must
    be labelled by the protocol decoder before becoming a rules payload.
    """
    if not isinstance(decoded, Mapping):
        return None

    # The outer transaction parser can legitimately fail on ObjFmt nested
    # records whose sibling count changes between client builds.  The raw
    # bytes are still a typed protocol payload: recover only the explicitly
    # labelled identifiers needed by the ingress adapter.  This is not a
    # heuristic scan of arbitrary UIDs; each value must follow a
    # SessionCardId/SourceCardId label.
    raw = decoded.get("__raw__")
    if isinstance(raw, memoryview):
        raw = raw.tobytes()
    elif isinstance(raw, bytearray):
        raw = bytes(raw)
    raw_payload = {}
    if isinstance(raw, bytes):
        # Nested SessionCardId values in AbilityTargetInstance can be
        # wrapped as ``value`` by the Mono client.  Use the shared typed-UID
        # decoder for this recovery path; the later choice handler still
        # checks the UID against its persisted legal-choice set.
        from rules_port.wire import extract_session_card_uids

        card_match = re.search(
            rb"(?:m_SessionCardId|SourceCardId|m_SourceCardId).*?"
            rb"m_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});", raw)
        if card_match:
            try:
                raw_payload["card_id"] = struct.unpack(
                    "<Q", bytes.fromhex(card_match.group(1).decode("ascii"))
                )[0]
                raw_payload["source_card_id"] = raw_payload["card_id"]
            except (ValueError, UnicodeDecodeError, struct.error):
                pass
        if b"ReadyCardTransaction" in raw:
            ready_ids = []
            for match in re.finditer(
                    rb"m_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});",
                    raw):
                try:
                    value = struct.unpack(
                        "<Q", bytes.fromhex(match.group(1).decode("ascii"))
                    )[0]
                except (ValueError, UnicodeDecodeError, struct.error):
                    continue
                if (value & 0xFF) == 1:
                    ready_ids.append(int(value))
            if ready_ids:
                raw_payload["card_ids"] = tuple(ready_ids)
        # CommitTroopsToAttackTransaction serializes AttackDeclaration as a
        # nested list. Some client builds use a SessionCardId ``value``
        # wrapper that the generic ObjFmt parser cannot walk; recover the
        # explicitly labelled declaration without treating unrelated UIDs as
        # cards.
        if ((getattr(command, "is_commit_attack", False) or
             b"CommitTroopsToAttackTransaction" in raw) and
                b"m_Attacks" in raw):
            def labelled_card(label, start=0):
                pos = raw.find(label, start)
                if pos < 0:
                    return None, pos
                match = re.search(
                    rb"m_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});",
                    raw[pos:])
                if not match:
                    return None, pos
                try:
                    value = struct.unpack(
                        "<Q", bytes.fromhex(match.group(1).decode("ascii")))[0]
                except (ValueError, UnicodeDecodeError, struct.error):
                    return None, pos
                return (int(value) if (value & 0xFF) == 1 else None), pos
            target, target_pos = labelled_card(b"DefendingCardId")
            attackers_pos = raw.find(b"AttackingCardIds")
            attackers = None
            if attackers_pos >= 0:
                # This is a List<SessionCardId>.  A no-attacks declaration is
                # encoded as an empty list (the count field is zero), so it
                # must remain a valid typed declaration rather than being
                # dropped as an undecodable payload.
                end = len(raw)
                for marker in (b"m_PlayerId", b"m_TransactionId"):
                    marker_pos = raw.find(marker, attackers_pos + len(b"AttackingCardIds"))
                    if marker_pos >= 0:
                        end = min(end, marker_pos)
                segment = raw[attackers_pos:end]
                if re.search(rb"AttackingCardIds;[^;]*;[^;]*;0;0;", segment):
                    attackers = ()
                else:
                    values = []
                    for match in re.finditer(
                            rb"m_UID64;[^;]*;[^;]*;[^;]*;([0-9A-Fa-f]{16});",
                            segment):
                        try:
                            value = struct.unpack(
                                "<Q", bytes.fromhex(match.group(1).decode("ascii"))
                            )[0]
                        except (ValueError, UnicodeDecodeError, struct.error):
                            continue
                        if (value & 0xFF) == 1:
                            values.append(int(value))
                    attackers = tuple(values) if values else None
            if target is not None and attackers is not None:
                # The client emits one declaration per transaction; preserve
                # the typed shape expected by the RulesPort, including no
                # attackers.
                raw_payload["declarations"] = ((target, attackers),)
        ability_match = re.search(
            rb"(?:AbilityTemplateId|m_AbilityTemplateId).*?"
            rb"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            rb"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})", raw)
        # Some Mono builds serialize ResourceId as a nested ``m_Guid``
        # value containing the 16 raw GUID bytes (ObjFmt renders that value
        # as 32 hexadecimal characters), rather than the dashed textual
        # form.  The outer label is still authoritative, so normalize only
        # the value immediately following AbilityTemplateId.
        if ability_match is None:
            guid_match = re.search(
                rb"(?:AbilityTemplateId|m_AbilityTemplateId).*?"
                # ResourceId.m_Guid is encoded by the client as
                # ``m_Guid;<type>;<count>;<flags>;<length>;<hex>``.  The
                # length field is the fourth metadata value after m_Guid;
                # older recovery code expected one extra value and therefore
                # dropped otherwise valid champion activations.
                rb"m_Guid;[^;]*;[^;]*;[^;]*;"
                rb"([0-9a-fA-F]{32});", raw)
            if guid_match:
                compact = guid_match.group(1).decode("ascii").lower()
                ability_match = re.match(
                    rb"([0-9a-fA-F]{8})([0-9a-fA-F]{4})([0-9a-fA-F]{4})"
                    rb"([0-9a-fA-F]{4})([0-9a-fA-F]{12})",
                    compact.encode("ascii"))
                if ability_match:
                    raw_payload["ability_template_id"] = (
                        f"{ability_match.group(1).decode()}-"
                        f"{ability_match.group(2).decode()}-"
                        f"{ability_match.group(3).decode()}-"
                        f"{ability_match.group(4).decode()}-"
                        f"{ability_match.group(5).decode()}")
        if ability_match is None:
            # The normal ObjFmt decoder can fail on the nested TargetMap
            # before exposing AbilityTemplateId.  Some client builds emit a
            # dashed GUID in that same envelope, so recover the explicitly
            # labelled value and continue with the typed target recovery.
            dashed_guid = re.search(
                rb"(?:AbilityTemplateId|m_AbilityTemplateId).*?"
                rb"m_Guid;[^;]*;[^;]*;[^;]*;"
                rb"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                rb"[0-9a-fA-F]{4}-[0-9a-fA-F]{12});", raw)
            if dashed_guid:
                ability_match = dashed_guid
                raw_payload["ability_template_id"] = \
                    dashed_guid.group(1).decode("ascii").lower()
        if ability_match:
            raw_payload.setdefault("ability_template_id",
                                   ability_match.group(1).decode("ascii").lower())
            # ActivationData is a typed nested record.  Preserve an explicit
            # empty record when the client supplied it; target decoding can
            # refine it in a later seam without dropping the activation.
            if b"AbilityActivationData" in raw:
                raw_payload["activation_data"] = {}
                # TargetMap is a nested generic dictionary that older ObjFmt
                # decoders cannot materialize.  For a class-23 discard
                # continuation the selected SessionCardId is the final Card
                # UID in the envelope; preserve it as target index 0.  Card
                # UIDs are explicitly typed (low byte == 1), so player or
                # ability identifiers are never promoted to targets.
                card_uids = list(extract_session_card_uids(raw))
                if len(card_uids) > 1:
                    # TargetMap and XCostData are distinct records.  Parse
                    # their labelled card fields independently; using the
                    # last UID in the envelope makes Bunoshi buff the troop
                    # selected for sacrifice and then fail the cost check.
                    def labelled_card(label):
                        pos = raw.find(label)
                        if pos < 0:
                            return None
                        values = extract_session_card_uids(raw[pos:])
                        if values:
                            return int(values[0])
                        return None
                    effect_target = labelled_card(b"TargetMap")
                    sacrifice_target = labelled_card(b"CardsToSacrifice")
                    if effect_target is None:
                        effect_target = card_uids[-1]
                    raw_payload["activation_data"] = {
                        "target_map": {0: [effect_target]}
                    }
                    if sacrifice_target is not None:
                        raw_payload["activation_data"]["cost_target_map"] = {
                            0: [sacrifice_target]
                        }

    def find(*names):
        """Find labelled fields through the decoded wrapper's nesting."""
        wanted = set(names)
        pending = [decoded]
        while pending:
            current = pending.pop()
            if isinstance(current, Mapping):
                for name in names:
                    if name in current:
                        return current[name]
                pending.extend(current.values())
            elif isinstance(current, (list, tuple)):
                pending.extend(current)
        return None

    payload = {}

    def unwrap(value):
        """Collapse ObjFmt UID/Guid/value wrappers to their scalar value."""
        while isinstance(value, Mapping):
            next_value = None
            for key in ("UID", "uid", "m_UID64", "Guid", "guid", "m_Guid", "value"):
                if key in value:
                    next_value = value[key]
                    break
            if next_value is None or next_value is value:
                break
            value = next_value
        return value

    card = find("CardId", "SourceCardId", "SessionCardId",
                "m_CardId", "m_SourceCardId", "m_SessionCardId")
    card = unwrap(card)
    if card is not None:
        payload["card_id"] = card
        payload["source_card_id"] = card
    ability = find("AbilityTemplateId", "m_AbilityTemplateId")
    ability = unwrap(ability)
    if ability is not None:
        payload["ability_template_id"] = ability
    activation = find("AbilityActivationData", "m_AbilityActivationData",
                      "activation_data", "ActivationData", "m_ActivationData")
    if activation is not None:
        payload["activation_data"] = _normalize_activation_data(activation)
    if (getattr(command, "is_play_troop", False) or
            getattr(command, "is_play_artifact", False) or
            getattr(command, "is_play_spell", False)):
        ability_data = find("AbilityDataList", "m_AbilityDataList",
                            "AbilityData", "m_AbilityData",
                            "ability_data")
        if ability_data is not None:
            payload["ability_data"] = _normalize_activation_data(ability_data)
        playing_for_free = find("PlayingForFree", "m_PlayingForFree",
                                "ForFree", "m_ForFree", "playing_for_free")
        if playing_for_free is not None:
            if isinstance(playing_for_free, str):
                payload["playing_for_free"] = playing_for_free.strip().lower() in {
                    "1", "true", "yes"
                }
            else:
                payload["playing_for_free"] = bool(playing_for_free)
    if getattr(command, "is_state_checksum", False):
        checksum = find("ChecksumData", "m_ChecksumData",
                         "SessionChecksumData", "checksum_data")
        if checksum is None:
            # Some ObjFmt versions flatten the data contract instead of
            # retaining the nested member name.  Only use explicitly named
            # checksum fields; never promote arbitrary integer fields.
            session_checksum = find("SessionChecksum", "m_SessionChecksum")
            player_checksums = find("PlayerChecksums", "m_PlayerChecksums")
            want_data = find("WantData", "m_WantData")
            if (session_checksum is not None or player_checksums is not None or
                    want_data is not None):
                checksum = {
                    "SessionChecksum": session_checksum,
                    "PlayerChecksums": player_checksums,
                    "WantData": want_data,
                }
        if checksum is not None:
            payload["checksum_data"] = _normalize_checksum_data(checksum)
    instance = find("AbilityInstanceId", "m_AbilityInstanceId")
    instance = unwrap(instance)
    if instance is not None:
        payload["ability_instance_id"] = instance
    def numeric_id(value):
        value = unwrap(value)
        if isinstance(value, Mapping):
            value = value.get("uid64", value.get("m_UID64",
                             value.get("value__", value.get("value", value))))
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def id_list(value):
        if not isinstance(value, (list, tuple)):
            return ()
        return tuple(item for item in (numeric_id(item) for item in value)
                     if item is not None)

    if getattr(command, "is_set_stops", False):
        payload["self_phases"] = id_list(find(
            "SelfTurnPhases", "m_SelfTurnPhases", "self_phases"))
        payload["opponent_phases"] = id_list(find(
            "OpponentTurnPhases", "m_OpponentTurnPhases", "opponent_phases"))
    if getattr(command, "is_set_auto_pass", False):
        active = find("AsActive", "m_AsActive", "as_active")
        passing = find("PassingState", "m_PassingState", "passing_state")
        payload["as_active"] = bool(active) if active is not None else False
        payload["passing_state"] = numeric_id(passing)
    if getattr(command, "is_quit_game", False):
        def bool_value(value, default=False):
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes"}
            return bool(value) if value is not None else default
        bugged = find("WasBugged", "m_WasBugged", "was_bugged")
        series = find("QuitEntireSeries", "m_QuitEntireSeries",
                      "quit_entire_series")
        if bugged is None:
            bugged = command.fields.get("m_WasBugged")
        if series is None:
            series = command.fields.get("m_QuitEntireSeries",
                                        getattr(command, "quit_series", None))
        payload["was_bugged"] = bool_value(bugged)
        payload["quit_entire_series"] = bool_value(series)

    if getattr(command, "is_commit_attack", False):
        declarations = find("Attacks", "m_Attacks", "declarations") or ()
        # Only override the raw-recovered declarations when the typed parser
        # actually produced a declaration.  The nested AttackDeclaration does
        # not decode through the generic ObjFmt walker, so ``find`` returns
        # nothing; assigning an empty tuple here then clobbered the recovered
        # declaration in the ``raw_payload.update(payload)`` merge below and
        # the client's attack was silently dropped (stuck in Select Attackers).
        if declarations:
            payload["declarations"] = tuple(
                (target, attackers)
                for item in declarations if isinstance(item, Mapping)
                for target in (numeric_id(item.get("DefendingCardId",
                                                   item.get("defending_card_id"))),)
                for attackers in (id_list(item.get("AttackingCardIds",
                                                    item.get("attacking_card_ids"))),)
                if target is not None)
    elif getattr(command, "is_commit_defense", False):
        declarations = find("DefenseDeclarations", "m_DefenseDeclarations",
                            "declarations") or ()
        if declarations:
            payload["declarations"] = tuple(
                (attacker, blockers)
                for item in declarations if isinstance(item, Mapping)
                for attacker in (numeric_id(item.get("AttackerId",
                                                   item.get("attacker_id"))),)
                for blockers in (id_list(item.get("DefendingCardIds",
                                                  item.get("defending_card_ids"))),)
                if attacker is not None)
    elif getattr(command, "is_assign_damage", False):
        assignments = find("AssignedDamageOrder", "m_AssignedDamageOrder",
                           "DamageOrder", "assignments") or ()
        if assignments:
            payload["assignments"] = tuple(
                (numeric_id(item.get("CombatId", item.get("combat_id"))),
                 id_list(item.get("CardIds", item.get("card_ids"))))
                for item in assignments if isinstance(item, Mapping)
                if numeric_id(item.get("CombatId", item.get("combat_id"))) is not None)
        else:
            # An empty order is the valid client representation for a combat
            # with no attackers/blockers. Preserve it as typed payload so the
            # RulesPort can consume the transaction and advance past
            # AssignDamage; returning None makes the boundary reject it.
            payload["assignments"] = ()
    if raw_payload:
        raw_payload.update(payload)
        payload = raw_payload
    if getattr(command, "is_encounter_mod_dialog", False):
        conversation = find("ConversationId", "m_ConversationId",
                            "conversation_id")
        conversation = unwrap(conversation)
        if conversation is None and isinstance(raw, bytes):
            conversation = extract_resource_guid(raw, "ConversationId")
        if conversation is not None:
            payload["conversation_id"] = conversation
    return payload or None


@dataclass(frozen=True)
class PlayerTransactionCommand:
    """Typed classification of a client PlayerTransaction request."""

    inner_bytes: bytes
    fields: dict
    transaction_id: int
    quit_series: str
    pass_turn_phase: int | None
    is_set_stops: bool
    is_mulligan_keep: bool
    is_mulligan_redraw: bool
    is_cheat: bool
    is_pass_priority: bool
    is_choose_pick: bool
    is_discard: bool
    is_play_resource: bool
    is_play_troop: bool
    is_play_artifact: bool
    is_play_spell: bool
    is_play_champion: bool
    is_quit_game: bool
    is_ready_card: bool
    is_request_player_options: bool
    is_state_checksum: bool
    is_tip_window_closed: bool
    is_ability_activate: bool
    is_activate_triggered_abilities: bool
    is_set_ability_data: bool
    is_commit_attack: bool
    is_commit_defense: bool
    is_set_auto_pass: bool
    is_cancel_auto_pass: bool
    is_assign_damage: bool
    is_priority_sync: bool
    is_encounter_mod_dialog: bool
    typed_payload: Mapping[str, Any] | None = None


def classify_player_transaction(inner_bytes, *, typed_payload=None):
    """Classify a raw 3029 payload without mutating game state."""
    if isinstance(inner_bytes, memoryview):
        inner_bytes = inner_bytes.tobytes()
    elif isinstance(inner_bytes, bytearray):
        inner_bytes = bytes(inner_bytes)
    raw = inner_bytes if isinstance(inner_bytes, bytes) else b""
    raw_lower = raw.lower()
    has = lambda name: name.lower().encode("ascii") in raw_lower
    fields = _transaction_fields(raw)
    transaction_id_text = fields.get("m_TransactionId", "?")
    try:
        transaction_id = (int(transaction_id_text, 16)
                          if transaction_id_text != "?" else -1)
    except (TypeError, ValueError):
        transaction_id = -1

    return PlayerTransactionCommand(
        inner_bytes=raw,
        fields=fields,
        transaction_id=transaction_id,
        quit_series=fields.get("m_QuitEntireSeries", "?"),
        pass_turn_phase=_extract_enum_int(raw, "m_TurnPhase"),
        is_set_stops=has("SetTurnPhasesTransaction"),
        is_mulligan_keep=has("AcceptStartingHand"),
        is_mulligan_redraw=(has("MulliganTransaction")
                            and not has("AcceptStartingHand")),
        is_cheat=(has("DebugAction") or has("DebugCheatTransaction")),
        is_pass_priority=has("PassPriorityTransaction"),
        is_choose_pick=(has("ChoosePlayTransaction")
                        or has("ChooseDrawTransaction")),
        is_discard=(has("Discard") and has("SessionCardId")),
        is_play_resource=has("PlayResourceTransaction"),
        is_play_troop=has("PlayTroopTransaction"),
        is_play_artifact=has("PlayArtifactTransaction"),
        is_play_spell=has("PlaySpellTransaction"),
        is_play_champion=has("PlayChampionTransaction"),
        is_quit_game=has("QuitGameTransaction"),
        is_ready_card=has("ReadyCardTransaction"),
        is_request_player_options=(
            has("RequestPlayerOptionsTransaction")),
        is_state_checksum=has("SendGameStateChecksumTransaction"),
        is_tip_window_closed=has("TipWindowClosed"),
        # Both C# activation transaction types carry the same nested member.
        # Use the serialized transaction class to keep a triggered batch from
        # entering the manual ActivateAbility normalization branch first.
        is_ability_activate=(has("ActivateAbilityTransaction") and
                             not has("ActivateTriggeredAbiliesTransaction")),
        is_activate_triggered_abilities=has(
            "ActivateTriggeredAbiliesTransaction"),
        is_set_ability_data=has("SetAbilityActivationDataTransaction"),
        is_commit_attack=has("CommitTroopsToAttackTransaction"),
        is_commit_defense=has("CommitTroopsToDefenseTransaction"),
        is_set_auto_pass=has("SetAutoPassTransaction"),
        is_cancel_auto_pass=has("CancelAutoPassTransaction"),
        is_assign_damage=has("AssignDamageOrderTransaction"),
        is_priority_sync=has("RequestPrioritySyncTransaction"),
        is_encounter_mod_dialog=has("EncounterModDialogTransaction"),
        typed_payload=typed_payload,
    )
