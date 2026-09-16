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
    bstate = bstate or {}
    bstate["resolving_owner_id"] = owner_user_id
    bstate["resolving_source_uid"] = card_uid
    if (getattr(session, "_rules_port_session", None) is not None or
            (bstate or {}).get("_rules_port_attached")):
        from rules_port.resolution import resolve_port_ability
        def resolve(handler, game, session, db, pl_t, ai_t, bstate,
                   ability_guid, source_uid, owner_id, target_map):
            return resolve_port_ability(
                handler, game, session, db, pl_t, ai_t, bstate,
                ability_guid, source_uid, owner_id, target_map=target_map)
    else:
        from .resolution import resolve_ability
        resolve = resolve_ability
    out = resolve(handler, game, session, db, pl_t, ai_t, bstate,
                  ag, card_uid, owner_user_id, {})
    _log(f"    Deathcry {ag[:8]} resolved from stack")
    return out




def resolve_deathcry(game, session, db, handler, pl_t, ai_t, card_uid, tpl_guid, bstate=None):
    """When a troop dies, resolve any Deathcry abilities.

    Uses the card instance's current ability list (including temporary grants),
    with the printed template list as a fallback for older instances. Filters
    to abilities marked as CardEnteredZone triggers whose gamedata trigger
    condition actually holds for a death (source Warzone -> destination
    Discard), so a Deploy (enters-play) trigger never fires as a Deathcry.
    """
    from .condition_engine import ConditionContext, trigger_condition_met

    from pvp_db import (db_template_ability_payload, db_card_ability_payload,
                        db_card_owner_id, db_ability_trigger_metadata,
                        db_ability_game_text, db_ability_activation_metadata,
                        db_set_card_abilities)
    template_payload = db_template_ability_payload(tpl_guid, conn=db)
    instance_payload = db_card_ability_payload(
        session.session_id, int(card_uid), conn=db)
    trow = (template_payload,) if template_payload is not None else None
    irow = (instance_payload,) if instance_payload is not None else None
    if not trow and not irow:
        return
    owner_id = db_card_owner_id(session.session_id, int(card_uid), conn=db) or 0
    import json as _json
    ability_lists = []
    for raw_list in ((trow[0] if trow else "[]"),
                     (irow[0] if irow else "[]")):
        try:
            parsed = _json.loads(raw_list or "[]")
        except (ValueError, TypeError, _json.JSONDecodeError):
            parsed = []
        if isinstance(parsed, list):
            ability_lists.append(parsed)
    aguids = []
    for ability_list in ability_lists:
        for ability_guid in ability_list:
            ability_guid = str(ability_guid).lower()
            if ability_guid not in aguids:
                aguids.append(ability_guid)
    trigger_guids = []
    for ag in aguids:
        trigger_meta = db_ability_trigger_metadata(ag, conn=db)
        activation_meta = db_ability_activation_metadata(ag, conn=db)
        mrow = ((trigger_meta[2], db_ability_game_text(ag, conn=db),
                 activation_meta[5] if activation_meta else None)
                if trigger_meta else None)
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
        # ONE-SHOT is an instance property.  Consume it after the Deathcry
        # resolves so the client loses the granted power together with the
        # server-side card ability list.
        consume = getattr(handler, "_remove_one_shot_ability", None)
        if callable(consume):
            try:
                consume(session, card_uid, ag, game, pl_t, ai_t, bstate)
            except Exception as exc:
                from ._shared import _log
                _log(f"    One-shot Deathcry cleanup failed for {ag[:8]}: {exc}")
        else:
            meta = db_ability_activation_metadata(ag, conn=db)
            if meta and int(meta[1] or 0) == 1 and irow:
                current = []
                try:
                    current = _json.loads(irow[0] or "[]")
                except (ValueError, TypeError, _json.JSONDecodeError):
                    pass
                current = [value for value in current
                           if str(value).lower() != ag]
                db_set_card_abilities(
                    session.session_id, int(card_uid), _json.dumps(current),
                    conn=db)
                db.commit()
