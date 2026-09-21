"""RulesPort-owned Stealth counter lifecycle.

``CardCounterTemplate 78db859d`` ("Stealth") is a client game-rules keyword.
The client appends three BuiltInResources ability templates to every card
template in ``Session.AddGameRulesAbilitiesToCardTemplate`` and evaluates them
as state-based rules once the counter exists:

* ``StealthCantAttackAbilityTemplateId`` (1730bd95) — "While you are Stealth,
  opposing troops can't attack."
* ``StealthSpellshieldAbilityTemplateId`` (7e8bae76) — "While you are Stealth,
  you have Spellshield."
* ``StealthRemovalAbilityTemplateId`` (5211025a) — "At the start of your turn,
  if you are Stealth, remove a stealth counter from this."

Those built-ins are not Records rows, so the server keeps the same three rules
here (the same shape as the Tunneling keyword service) and applies them to PvE
and PvP alike.  Card text and counter names are never parsed: the counter
template identity and the turn boundary are the whole contract.
"""

from __future__ import annotations

from .runtime_helpers import (champion_uids_by_owner, pvp_opponent_pid,
                              raw_uid)


# CardCounterTemplate "Stealth" (gamedata).
STEALTH_COUNTER_GUID = "78db859d-02b9-fc97-e2fb-8aca1dfeed77"
# Client BuiltInResources ability templates listed above.
STEALTH_CANT_ATTACK_ABILITY_GUID = "1730bd95-7c2a-4a19-9d6d-80c9c22bb720"
STEALTH_SPELLSHIELD_ABILITY_GUID = "7e8bae76-202f-4225-84ef-dde8339ccff2"
STEALTH_REMOVAL_ABILITY_GUID = "5211025a-3b31-48d6-8180-14552e9404a2"


def stealth_counters(battle_state, champion_uid):
    """Return the stealth counters persisted on one champion."""
    try:
        key = str(raw_uid(champion_uid))
    except (TypeError, ValueError):
        return 0
    values = ((battle_state or {}).get("champion_counters") or {}).get(key, {})
    if not isinstance(values, dict):
        return 0
    try:
        return int(values.get(STEALTH_COUNTER_GUID, 0) or 0)
    except (TypeError, ValueError):
        return 0


def is_stealthed(battle_state, champion_uid):
    """Whether a champion is currently M:Stealth (one or more counters)."""
    return stealth_counters(battle_state, champion_uid) > 0


def champion_target_is_spellshielded(battle_state, card):
    """Whether a champion target candidate is protected by Stealth.

    ``card`` is one candidate row from the target scan; only champions carry
    counters in the persisted champion map.
    """
    if str((card or {}).get("card_type") or "") != "Champion":
        return False
    return is_stealthed(battle_state, (card or {}).get("card_uid"))


def advance(context, owner_id):
    """Client built-in ``StealthRemovalAbilityTemplateId``.

    "At the start of your turn, if you are Stealth, remove a stealth counter
    from this."  The removal is a state-based keyword step rather than a chain
    ability, so it runs in the same turn-boundary service as Tunneling.
    Returns ``[(champion_uid, old, new)]`` for the projected changes.
    """
    from .runtime_helpers import champion_uid_for_owner
    from .counter_effects import change_counter

    champion = champion_uid_for_owner(context.handler, context.bstate, owner_id)
    if champion is None or not is_stealthed(context.bstate, champion):
        return []
    old, new = change_counter(context, champion, "stealth",
                              STEALTH_COUNTER_GUID, 1, "remove")
    return [(champion, old, new)]


def _opposing_owner(battle_state, owner_id, champions):
    try:
        owner = int(owner_id if owner_id is not None else 0)
    except (TypeError, ValueError):
        return None
    if (battle_state or {}).get("pvp"):
        return pvp_opponent_pid(battle_state, owner)
    others = [pid for pid in champions if pid != owner]
    return others[0] if others else None


def defending_champion_is_stealthed(adapter, attacker, defender):
    """Client built-in ``StealthCantAttackAbilityTemplateId``.

    A stealthed champion cannot be attacked by opposing troops, so the
    declaration is illegal whether the defender arrived as a synthetic
    champion SessionCardId or as the champion face (``defender`` is None for
    champions because they have no ``game_cards`` row).
    """
    battle_state = getattr(adapter, "battle_state", None) or {}
    champions = champion_uids_by_owner(adapter, battle_state)
    if not champions:
        return False
    if defender is not None:
        try:
            uid = raw_uid(getattr(defender, "session_card_id", defender))
        except (TypeError, ValueError):
            return False
        if uid not in champions.values():
            return False  # an ordinary troop is a legal defender
        return is_stealthed(battle_state, uid)
    opponent = _opposing_owner(battle_state, getattr(attacker, "owner_id", None),
                               champions)
    champion = champions.get(opponent)
    return champion is not None and is_stealthed(battle_state, champion)
