"""Deathcry trigger resolution.

When a troop dies, its Deathcry trigger (if any) fires.  The effect is resolved
off the stack through the generic ability-resolution engine.
"""

import random as _random
import re as _re
import json as _json
import game_engine

from .effects.search import move_deck_card_to_hand


def _leaf_param(param):
    """Parse an ability_effects.param JSON blob (parent-level child params)."""
    if not param:
        return None
    try:
        d = _json.loads(param)
        return d if isinstance(d, dict) else None
    except (ValueError, TypeError):
        return None


def _resolve_deathcry_effect(game, session, db, handler, pl_t, ai_t, bstate,
                             card_uid, tpl_guid, owner_user_id, ag, gtext):
    """Resolve one Deathcry trigger through the authoritative resolution
    engine (effect groups, gamedata conditions, ability variables, target
    templates, and ActivateAbility recursion)."""
    from ._shared import _log
    from .resolution import resolve_ability
    bstate = bstate or {}
    bstate["resolving_owner_id"] = owner_user_id
    bstate["resolving_source_uid"] = card_uid
    out = resolve_ability(handler, game, session, db, pl_t, ai_t, bstate,
                          ag, card_uid, owner_user_id, {})
    _log(f"    Deathcry {ag[:8]} resolved from stack")
    return out




def resolve_deathcry(game, session, db, handler, pl_t, ai_t, card_uid, tpl_guid, bstate=None):
    """When a troop dies, resolve any Deathcry abilities.

    Looks up the card's abilities from card_templates.abilities_json, filters to
    those marked as CardEnteredZone triggers whose gamedata trigger condition
    actually holds for a death (source Warzone -> destination Discard), so a
    Deploy (enters-play) trigger never fires as a Deathcry, and resolves each.
    """
    from .condition_engine import ConditionContext, trigger_condition_met

    trow = db.execute(
        "SELECT abilities_json FROM card_templates WHERE guid=?",
        (tpl_guid,)).fetchone()
    if not trow or not trow[0]:
        return
    row2 = db.execute(
        "SELECT user_id FROM game_cards WHERE session_id=? AND card_uid=?",
        (session.session_id, int(card_uid))).fetchone()
    owner_id = row2[0] if row2 else 0
    import json as _json
    try:
        aguids = _json.loads(trow[0])
    except (ValueError, TypeError):
        return
    trigger_guids = []
    for ag in aguids:
        mrow = db.execute(
            "SELECT trigger_event_type, game_text, raw_json FROM card_abilities_meta "
            "WHERE ability_guid=?", (ag,)).fetchone()
        if not mrow or not mrow[0]:
            continue
        if "CardEnteredZone" in (mrow[0] or ""):
            raw = mrow[2] or ""
            if raw:
                # The trigger must hold for THIS card entering the discard pile
                # (kill_troop already moved the card before calling us), so a
                # Deploy "enters play" trigger is filtered out data-driven.
                try:
                    uses_previous_state = bool(
                        _json.loads(raw).get("m_UsesPreviousState", 0))
                except (TypeError, ValueError, _json.JSONDecodeError):
                    uses_previous_state = False
                ctx = ConditionContext(
                    db, session, bstate or {}, event_type="CardEnteredZoneEvent",
                    ability_source_uid=int(card_uid),
                    ability_source_owner_id=owner_id,
                    trigger_uid=int(card_uid),
                    pl_t=pl_t, ai_t=ai_t,
                    event_source_collection="warzone",
                    event_destination_collection="discard",
                    event_previous_state=game_engine.ECardStates.Dead,
                    uses_previous_state=uses_previous_state)
                if not trigger_condition_met(raw, ctx):
                    continue
            trigger_guids.append((ag, mrow[1] or ""))
    if not trigger_guids:
        return
    for ag, gtext in trigger_guids:
        _resolve_deathcry_effect(game, session, db, handler, pl_t, ai_t, bstate,
                                 card_uid, tpl_guid, owner_id, ag, gtext)
