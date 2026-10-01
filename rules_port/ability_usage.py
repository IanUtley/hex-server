"""Authored per-game ability usage shared by battle projections."""

from __future__ import annotations


def remaining_uses_per_game(db, session_id, source_uid, ability_guids,
                            battle_state=None, *, champion=False):
    """Return only the remaining-use entries C# includes in CardUpdated.

    C# stores per-game counts on each card instance and projects remaining
    uses, not activations used. Champion powers have synthetic card IDs, so
    their counts live in the serialized battle checkpoint instead.
    """
    try:
        source = int(getattr(source_uid, "uid64", source_uid))
    except (TypeError, ValueError):
        return {}
    from gamedata import DEFAULT_RECORD_STORE, ability_graph
    from pvp_db import db_card_ability_use_counts

    state = battle_state if isinstance(battle_state, dict) else {}
    champion_uses = state.get("champion_ability_uses") or {}
    remaining = {}
    for raw_guid in ability_guids or ():
        guid = str(getattr(raw_guid, "guid", raw_guid) or "").lower()
        if not guid:
            continue
        graph = ability_graph(DEFAULT_RECORD_STORE, guid)
        limit = int(getattr(getattr(graph, "costs", None),
                            "uses_per_game", 0) or 0)
        if limit <= 0:
            continue
        if champion:
            used = champion_uses.get(
                f"{source}:{guid}", champion_uses.get(guid, 0))
            try:
                used = int(used or 0)
            except (TypeError, ValueError):
                used = 0
        else:
            used, _turn = db_card_ability_use_counts(
                session_id, source, guid,
                state.get("turn_number", 1), conn=db)
        if used > 0 and used < limit:
            remaining[guid] = limit - used
    return remaining
