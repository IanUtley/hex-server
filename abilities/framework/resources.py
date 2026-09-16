"""Shared resolution for abilities dynamically granted to resource cards."""

from collections import Counter
import json

from gamedata import DEFAULT_RECORD_STORE, ability_graph


_RECORD_STORE = DEFAULT_RECORD_STORE


def printed_resource_choice_ability(ability_guids):
    """Return a printed resource ability that opens a choice-card picker.

    Some resources do not have a colour in their name.  Their threshold is
    supplied by a metadata ability that creates temporary cards in the
    ``Choosing`` zone and invokes ``ChooseAndPlay``.  Keep resource-play code
    independent of card names by recognizing that shape in the authoritative
    ability graph instead of parsing card text.
    """
    for value in ability_guids or []:
        guid = str(value or "").lower()
        if not guid:
            continue
        graph = ability_graph(_RECORD_STORE, guid)
        if graph is None:
            continue
        has_choice_tokens = False
        has_activation = False
        for effect in graph.effects:
            if effect.concrete_type == "ActivateAbilityEffectTemplate":
                has_activation = True
            elif effect.concrete_type == "SummonTokenTroopAbilityEffectTemplate":
                template = effect.template
                collection = (template.field("m_CardCollection", "")
                              if template is not None else "")
                if str(collection).rsplit(".", 1)[-1].lower() == "choosing":
                    has_choice_tokens = True
        if has_choice_tokens and has_activation:
            return guid
    return None


def resolve_granted_resource_abilities(game, session, db, handler, pl_t, ai_t,
                                       bstate, card_uid, owner_id,
                                       *, resolver=None):
    """Resolve non-triggered abilities added to a resource card instance.

    Printed resource abilities are already handled by the resource-play paths
    (threshold, charge, and the template's resource-pool grants).  A card such
    as Fruitful Foresight adds a new ability to the next resource while it is
    still in the deck.  The client executes that added ability when the
    resource is played, so resolve the instance-only portion here using the
    same metadata/BOM interpreter in every game mode.
    """
    from pvp_db import db_card_ability_state
    row = db_card_ability_state(session.session_id, int(card_uid), conn=db)
    if not row:
        return []
    try:
        current = [str(g).lower() for g in json.loads(row[0] or "[]")]
    except (TypeError, ValueError, json.JSONDecodeError):
        current = []
    try:
        printed = [str(g).lower() for g in json.loads(row[1] or "[]")]
    except (TypeError, ValueError, json.JSONDecodeError):
        printed = []

    # Preserve multiplicity: GrantAbility may intentionally add the same
    # non-unique ability more than once, and each copy is a separate effect.
    granted = Counter(current)
    granted.subtract(Counter(printed))
    dynamic_guids = []
    for guid in current:
        if granted[guid] > 0:
            dynamic_guids.append(guid)
            granted[guid] -= 1

    if not dynamic_guids:
        return []

    port_session = getattr(session, "_rules_port_session", None)
    if resolver is not None:
        resolve = resolver
    elif port_session is not None:
        from rules_port.resolution import resolve_port_ability
        def resolve(handler, game, session, db, pl_t, ai_t, bstate,
                   ability_guid, source_uid, owner_id, target_map):
            return resolve_port_ability(
                handler, game, session, db, pl_t, ai_t, bstate,
                ability_guid, source_uid, owner_id, target_map=target_map)
    else:
        from .resolution import resolve_ability
        resolve = resolve_ability

    logs = []
    for ability_guid in dynamic_guids:
        graph = ability_graph(_RECORD_STORE, ability_guid)
        if graph is None or graph.manual or graph.trigger_event_type:
            # Manual abilities remain activatable; event-driven abilities are
            # dispatched by their normal event path rather than on play.
            continue
        result = resolve(
            handler, game, session, db, pl_t, ai_t, bstate,
            ability_guid, int(card_uid), owner_id, {})
        logs.append(f"{ability_guid[:8]}: {result}")
    return logs


def resolve_printed_resource_abilities(game, session, db, handler, pl_t, ai_t,
                                       bstate, card_uid, owner_id,
                                       skip_guids=()):
    """Resolve non-triggered printed resource abilities when they are played.

    Resource play has a normal rules path for the basic threshold and charge
    grants.  Other printed BOM abilities still need to run at that same point
    (for example Starsphere's reveal and Shard of Cunning's threshold choice).
    Identify the already-handled leaves by their typed effect properties rather
    than by card names or display text.
    """
    from pvp_db import db_card_ability_state
    row = db_card_ability_state(session.session_id, int(card_uid), conn=db)
    if not row:
        return []
    try:
        current = [str(g).lower() for g in json.loads(row[0] or "[]")]
    except (TypeError, ValueError, json.JSONDecodeError):
        current = []
    skip = {str(g).lower() for g in (skip_guids or ())}

    port_session = getattr(session, "_rules_port_session", None)
    if port_session is not None:
        from rules_port.resolution import resolve_port_ability
        def resolve(handler, game, session, db, pl_t, ai_t, bstate,
                   ability_guid, source_uid, owner_id, target_map):
            return resolve_port_ability(
                handler, game, session, db, pl_t, ai_t, bstate,
                ability_guid, source_uid, owner_id, target_map=target_map)
    else:
        from .resolution import resolve_ability
        resolve = resolve_ability

    logs = []
    for guid in current:
        if not guid or guid in skip:
            continue
        graph = ability_graph(_RECORD_STORE, guid)
        if graph is None or graph.manual or graph.trigger_event_type:
            continue
        from pvp_db import db_ability_effect_type_params
        effects = db_ability_effect_type_params(guid, conn=db)
        if effects and all(
                effect_type == "CardModifierAbilityEffectTemplate"
                and _resource_grant_property(param)
                for effect_type, param in effects):
            # These are applied by the ordinary resource-play rules above.
            continue
        result = resolve(
            handler, game, session, db, pl_t, ai_t, bstate,
            guid, int(card_uid), owner_id, {})
        logs.append(f"{guid[:8]}: {result}")
    return logs


def _resource_grant_property(param):
    try:
        value = json.loads(param or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return str(value.get("property") or "").lower() in {
        "threshold", "chargepoints",
    }


# Compatibility names intentionally delegate to the single RulesPort
# implementation.  Older callers may still import this module, but they no
# longer get a second resource-rules implementation at runtime.
def printed_resource_choice_ability(ability_guids):
    from rules_port.resources import printed_resource_choice_ability as impl
    return impl(ability_guids)


def resolve_granted_resource_abilities(game, session, db, handler, pl_t, ai_t,
                                       bstate, card_uid, owner_id, *,
                                       resolver=None):
    from rules_port.resources import resolve_granted_resource_abilities as impl
    return impl(game, session, db, handler, pl_t, ai_t, bstate, card_uid,
                owner_id, resolver=resolver)


def resolve_printed_resource_abilities(game, session, db, handler, pl_t, ai_t,
                                       bstate, card_uid, owner_id,
                                       skip_guids=()):
    from rules_port.resources import resolve_printed_resource_abilities as impl
    return impl(game, session, db, handler, pl_t, ai_t, bstate, card_uid,
                owner_id, skip_guids=skip_guids)
