"""Abilities package for Hex TCG — champion ability effects.

Champion charge powers (PvP) and talent abilities (PvE) are **data-driven**
through the BOM (Bill of Materials) pattern.  Most effects don't need custom
code — the ``ability_effects`` table defines an ordered chain of leaf effects
executed by ``framework/bom.py``.

Custom handlers (``@register_custom_ability``) go in ``abilities/cards/`` and
are only needed for effects not expressible in the BOM.

Public API::

    from abilities import resolve_effect, resolve_ability, resolve_played_spell

    # Get an effect function for a top-level ability GUID
    fn = resolve_effect(ability_guid)
    if fn:
        log = fn(game, session, db, handler, pl_t, ai_t, bstate, guid, source_scid)

Discovery::

    from abilities import discover_abilities
    discover_abilities()   # auto-imports abilities/cards/ modules

Adding a new card ability::

    # abilities/cards/my_ability.py
    from abilities.registry import register_custom_ability

    @register_custom_ability("ability-guid-here")
    def my_ability(game, session, db, handler, pl_t, ai_t, bstate, guid, scid):
        ...
        return "log message"
"""

from .framework.bom import (_LEAFS, _walk_bom, leaf_register, bom_has_leaf,
                            bom_has_discard, bom_leaf_prompt_data)
from .framework.tac import decode_tac, tac_guid, tac_function
from .framework.conditions import register_condition, evaluate_condition, apply_pregame_abilities
from .framework.kill_troop import kill_troop, state_based_deaths
from .framework.transform import transform_card
from .framework.deathcry import resolve_deathcry, _resolve_deathcry_effect
from .framework.stat_mod import apply_card_stat_mod
from .framework.triggers import (
    resolve_triggers,
    resolve_enters_play_triggers,
    resolve_stack_trigger,
)
from .framework._shared import _stat_delta
from .registry import register_custom_ability, lookup, discover as _discover_cards


def resolve_effect(ability_guid):
    """Return an effect function for a top-level ability GUID, or None.

    Prefers a registered custom handler; otherwise walks the ability_effects
    BOM through the authoritative resolution engine and returns a wrapper.
    """
    custom = lookup(ability_guid)
    if custom:
        return custom

    def _bom_effect(game, session, db, handler, pl_t, ai_t, bstate, ability_guid_, source_scid):
        from .framework.resolution import resolve_ability
        bstate = bstate or {}
        src = (bstate.get("resolving_source_uid")
               if bstate.get("resolving_source_uid") is not None
               else source_scid)
        owner = bstate.get("resolving_owner_id")
        if owner is None and src is not None:
            try:
                orow = db.execute(
                    "SELECT user_id FROM game_cards "
                    "WHERE session_id=? AND card_uid=?",
                    (session.session_id, int(src))).fetchone()
                owner = orow[0] if orow else 0
            except Exception:
                owner = 0
        out = resolve_ability(handler, game, session, db, pl_t, ai_t, bstate,
                              ability_guid_, src, owner, {})
        return f"[{ability_guid_}] " + (out or "")

    try:
        import db as _dbmod
        rows = _dbmod._db.execute(
            "SELECT 1 FROM ability_effects WHERE ability_guid=? LIMIT 1",
            (ability_guid,)).fetchone()
    except Exception:
        rows = None
    return _bom_effect if rows else None


def resolve_played_spell(game, session, db, handler, pl_t, ai_t, bstate, ability_guids):
    """Resolve a played spell (BasicAction/QuickAction) by walking each ability's
    BOM through the authoritative resolution engine — effect groups, gamedata
    conditions, ability variables, target templates and ActivateAbility
    recursion all resolve data-driven, with the chosen spell target feeding the
    activation TargetMap.

    Returns a log string.
    """
    from .framework.resolution import resolve_ability

    bstate = bstate or {}
    target_uid = bstate.get("player_spell_target")
    src_uid = bstate.get("resolving_source_uid")
    owner_id = bstate.get("resolving_owner_id")
    if owner_id is None and src_uid is not None:
        try:
            orow = db.execute(
                "SELECT user_id FROM game_cards "
                "WHERE session_id=? AND card_uid=?",
                (session.session_id, int(src_uid))).fetchone()
            owner_id = orow[0] if orow else 0
        except Exception:
            owner_id = 0
    if owner_id is None:
        # No source card (test harness / direct call): the spell was cast by
        # the human controller unless the caller said otherwise.
        owner_id = handler.user_profile["id"] if handler.user_profile else 0
    logs = []
    # Scope the "escalation counted this cast" marker to THIS resolution so a
    # stale flag from an earlier cast can never suppress the next escalation.
    prev_esc_flag = bstate.get("_esc_counted_this_resolution")
    bstate["_esc_counted_this_resolution"] = False
    try:
        for ag_str in (ability_guids or []):
            ag = str(ag_str)
            has_rows = db.execute(
                "SELECT 1 FROM ability_effects WHERE ability_guid=? LIMIT 1",
                (ag,)).fetchone()
            if has_rows:
                target_map = {0: int(target_uid)} if target_uid is not None else {}
                out = resolve_ability(handler, game, session, db, pl_t, ai_t,
                                      bstate, ag, src_uid, owner_id, target_map)
                logs.append(out)
            else:
                # Legacy fallback: no BOM rows for this ability — apply the
                # text-derived stat delta against the chosen target so old cards
                # without extracted effect data still resolve.
                from .framework._shared import _stat_delta, _log
                from .framework.stat_mod import apply_card_stat_mod
                bstate["resolving_ability"] = ag
                trow = db.execute(
                    "SELECT game_text FROM card_abilities_meta WHERE ability_guid=?",
                    (ag,)).fetchone()
                game_text = trow[0] if trow else ""
                atk_d = _stat_delta(game_text, "ATK")
                def_d = _stat_delta(game_text, "DEF")
                if target_uid:
                    apply_card_stat_mod(game, session, db, handler, pl_t, ai_t,
                                        target_uid, atk_d, def_d)
                    logs.append(f"spell {ag[:8]} -> {hex(int(target_uid))} "
                                f"{atk_d:+}/{def_d:+}")
                bstate.pop("resolving_ability", None)
    finally:
        if prev_esc_flag is None:
            bstate.pop("_esc_counted_this_resolution", None)
        else:
            bstate["_esc_counted_this_resolution"] = prev_esc_flag
    return "; ".join(str(l) for l in logs if l)


def _parse_bom_param(param):
    """Return the CardModifier 'property' from a BOM leaf's param JSON."""
    import json
    if not param:
        return None
    try:
        d = json.loads(param)
        return d.get("property") if isinstance(d, dict) else None
    except (ValueError, TypeError):
        return None


def discover_abilities():
    """Auto-discover custom ability handlers in ``abilities/cards/``.
    Call once at startup.
    """
    _discover_cards()
