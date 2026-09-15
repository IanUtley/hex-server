"""RulesPort-owned per-turn and game card-cast statistics."""

from __future__ import annotations


def _side(owner_id, battle_state):
    if battle_state.get("pvp"):
        return f"owner_{int(owner_id)}"
    return "player" if int(owner_id or 0) else "ai"


def record_card_cast(battle_state: dict, owner_id: int, *, resource: bool,
                     turn_number: int | None = None) -> None:
    """Record one accepted card play for typed condition evaluation."""
    side = _side(owner_id, battle_state)
    if turn_number is None:
        turn_number = int(battle_state.get("turn_number", 1) or 1)
    marker = f"{side}_cards_cast_turn"
    if battle_state.get(marker) != int(turn_number):
        battle_state[f"{side}_cards_cast_this_turn"] = 0
        battle_state[f"{side}_resource_cards_cast_this_turn"] = 0
        battle_state[f"{side}_nonresource_cards_cast_this_turn"] = 0
        battle_state[marker] = int(turn_number)
    battle_state[f"{side}_cards_cast_this_turn"] = int(
        battle_state.get(f"{side}_cards_cast_this_turn", 0) or 0) + 1
    kind = "resource" if resource else "nonresource"
    turn_key = f"{side}_{kind}_cards_cast_this_turn"
    battle_state[turn_key] = int(battle_state.get(turn_key, 0) or 0) + 1
    total_key = f"{side}_cards_cast"
    battle_state[total_key] = int(battle_state.get(total_key, 0) or 0) + 1
    total_kind = f"{side}_{kind}_cards_cast"
    battle_state[total_kind] = int(battle_state.get(total_kind, 0) or 0) + 1
