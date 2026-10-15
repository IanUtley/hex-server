"""Round-scheduled campaign scene mods.

A scene mod entry with a ``target`` and ``round > 0`` is not placed at battle
start.  Setup holds each of its cards in ``bstate["pending_scene_round_mods"]``
and the grant is applied when the target side starts its Nth turn, where N is
the entry's ``round``.  "Round N" means that side's Nth turn.

The grant is read from the card's authored resource modifiers (the
``CardModifierAbilityEffectTemplate`` rows with a currentresource or
totalresource property), so no card values are hard-coded.
"""

import json

from pvp_db import db_ability_effect_rows, db_template_ability_guids
from .resources import project_resource_change

PENDING_KEY = "pending_scene_round_mods"
TURN_STARTS_KEY = "scene_round_turn_starts"
TARGET_SIDES = {"AIPlayer": "ai", "Player": "player"}
RESOURCE_PROPERTIES = ("currentresource", "totalresource")


def split_round_mods(scene_mods):
    """Separate scheduled entries from the mods placed at battle start.

    Returns ``(immediate, pending)``.  ``immediate`` keeps every entry that
    setup should place as before.  ``pending`` lists one dict per scheduled
    card: ``{"round", "side", "card"}``.
    """
    immediate, pending = [], []
    for mod in scene_mods or []:
        if not isinstance(mod, dict):
            continue
        side = TARGET_SIDES.get(str(mod.get("target") or ""))
        try:
            scheduled_round = int(mod.get("round", 0) or 0)
        except (TypeError, ValueError):
            scheduled_round = 0
        if side is None or scheduled_round <= 0:
            immediate.append(mod)
            continue
        for item in mod.get("mods") or []:
            guid = str((item or {}).get("guid") or "").lower()
            if guid:
                pending.append({"round": scheduled_round, "side": side,
                                "card": guid})
    return immediate, pending


def count_turn_start(state, side):
    """Record that ``side`` ("player" or "ai") started a turn; return its count."""
    counts = state.setdefault(TURN_STARTS_KEY, {"player": 0, "ai": 0})
    counts[side] = int(counts.get(side, 0) or 0) + 1
    return counts[side]


def card_resource_grants(db, card_guid):
    """Return ``[(property, amount)]`` from a card's authored resource effects."""
    grants = []
    raw_guids = db_template_ability_guids(card_guid, conn=db) or "[]"
    for ability_guid in json.loads(raw_guids):
        for _effect_guid, effect_type, raw_param in db_ability_effect_rows(
                ability_guid, conn=db):
            if effect_type != "CardModifierAbilityEffectTemplate":
                continue
            try:
                param = json.loads(raw_param or "{}")
            except (TypeError, ValueError):
                continue
            prop = str(param.get("property") or "").lower()
            amount = int(param.get("amount") or 0)
            if prop in RESOURCE_PROPERTIES and amount:
                grants.append((prop, amount))
    return grants


def apply_due_round_mods(db, game, session, state, side, turn_count,
                         player_uid, ai_uid):
    """Apply and retire every pending entry due on this side's Nth turn."""
    pending = state.get(PENDING_KEY) or []
    due = [entry for entry in pending
           if entry["side"] == side and int(entry["round"]) == int(turn_count)]
    if not due:
        return []
    applied = []
    for entry in due:
        for prop, amount in card_resource_grants(db, entry["card"]):
            project_resource_change(
                game, session, state, player_uid, ai_uid,
                entry["side"], prop, amount)
            applied.append((entry["card"], prop, amount))
        pending.remove(entry)
    state[PENDING_KEY] = pending
    return applied
