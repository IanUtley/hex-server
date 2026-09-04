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
from gamedata import (ActivationData, RecordStore, ability_graph)
from .registry import register_custom_ability, lookup, discover as _discover_cards


_RECORD_STORE = RecordStore()


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

    if ability_graph(_RECORD_STORE, str(ability_guid).lower()) is None:
        return None
    return _bom_effect


def resolve_played_spell(game, session, db, handler, pl_t, ai_t, bstate,
                         ability_guids, activations=None):
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
        record_store = _RECORD_STORE
        activation_map = {str(key).lower(): ActivationData.from_dict(value)
                          for key, value in (activations or {}).items()}
        for ag_str in (ability_guids or []):
            ag = str(ag_str)
            graph = ability_graph(record_store, ag.lower())
            if graph is None:
                raise RuntimeError(
                    f"played ability {ag.lower()} is missing from current Records")
            activation = activation_map.get(ag.lower())
            target_map = (dict(activation.target_map)
                          if activation is not None else {})
            if not target_map and target_uid is not None:
                # Card-play supplies one chosen spell target. Bind it to the
                # first explicit target template in the ability graph; automatic
                # target templates are resolved by the ability instance itself.
                for index, target in enumerate(graph.targets):
                    if target.requires_input:
                        target_map[index] = int(target_uid)
                        break
            out = resolve_ability(
                handler, game, session, db, pl_t, ai_t, bstate, ag, src_uid,
                owner_id, target_map,
                variables=(activation.variables if activation is not None
                           else None), activation_data=activation)
            logs.append(out)
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
