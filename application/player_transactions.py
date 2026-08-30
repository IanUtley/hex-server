"""Classification of GameSession PlayerTransaction payloads."""

from dataclasses import dataclass


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
    is_ability_activate: bool
    is_set_ability_data: bool
    is_commit_attack: bool
    is_commit_defense: bool
    is_set_auto_pass: bool
    is_cancel_auto_pass: bool
    is_assign_damage: bool
    is_priority_sync: bool


def classify_player_transaction(inner_bytes):
    """Classify a raw 3029 payload without mutating game state."""
    raw = inner_bytes if isinstance(inner_bytes, bytes) else b""
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
        is_set_stops=b"SetTurnPhasesTransaction" in raw,
        is_mulligan_keep=b"AcceptStartingHand" in raw,
        is_mulligan_redraw=(b"MulliganTransaction" in raw
                            and b"AcceptStartingHand" not in raw),
        is_cheat=(b"DebugAction" in raw or b"DebugCheatTransaction" in raw),
        is_pass_priority=b"PassPriorityTransaction" in raw,
        is_choose_pick=(b"ChoosePlayTransaction" in raw
                        or b"ChooseDrawTransaction" in raw),
        is_discard=(b"Discard" in raw and b"SessionCardId" in raw),
        is_ability_activate=b"m_AbilityActivationData" in raw,
        is_set_ability_data=b"SetAbilityActivationDataTransaction" in raw,
        is_commit_attack=b"CommitTroopsToAttackTransaction" in raw,
        is_commit_defense=b"CommitTroopsToDefenseTransaction" in raw,
        is_set_auto_pass=b"SetAutoPassTransaction" in raw,
        is_cancel_auto_pass=b"CancelAutoPassTransaction" in raw,
        is_assign_damage=b"AssignDamageOrderTransaction" in raw,
        is_priority_sync=b"RequestPrioritySyncTransaction" in raw,
    )
