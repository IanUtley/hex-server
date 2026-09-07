"""Shared resolution for abilities dynamically granted to resource cards."""

from collections import Counter
import json

from gamedata import DEFAULT_RECORD_STORE, ability_graph


_RECORD_STORE = DEFAULT_RECORD_STORE


def resolve_granted_resource_abilities(game, session, db, handler, pl_t, ai_t,
                                       bstate, card_uid, owner_id):
    """Resolve non-triggered abilities added to a resource card instance.

    Printed resource abilities are already handled by the resource-play paths
    (threshold, charge, and the template's resource-pool grants).  A card such
    as Fruitful Foresight adds a new ability to the next resource while it is
    still in the deck.  The client executes that added ability when the
    resource is played, so resolve the instance-only portion here using the
    same metadata/BOM interpreter in every game mode.
    """
    row = db.execute(
        "SELECT gc.card_abilities, ct.abilities_json "
        "FROM game_cards gc JOIN card_templates ct "
        "ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.card_uid=?",
        (session.session_id, int(card_uid))).fetchone()
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

    from .resolution import resolve_ability

    logs = []
    for ability_guid in dynamic_guids:
        graph = ability_graph(_RECORD_STORE, ability_guid)
        if graph is None or graph.manual or graph.trigger_event_type:
            # Manual abilities remain activatable; event-driven abilities are
            # dispatched by their normal event path rather than on play.
            continue
        result = resolve_ability(
            handler, game, session, db, pl_t, ai_t, bstate,
            ability_guid, int(card_uid), owner_id, {})
        logs.append(f"{ability_guid[:8]}: {result}")
    return logs
