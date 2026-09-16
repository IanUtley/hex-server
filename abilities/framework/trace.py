"""Opt-in, JSON-safe traces of production ability resolution.

The trace is deliberately built from the same SQLite rows and events used by
the live server.  It is not a second simulator: a catalog test can therefore
assert that an authored Bury effect moved exactly N rows from Deck to Discard
after the normal resolver actually ran.
"""

from __future__ import annotations

from typing import Any
import json


# Every mutable persisted card field.  Keep this list alongside static.py's
# game_cards schema: the trace is an audit projection, so omitting a newly
# added mutable field would create a false "no change" result.
_CARD_COLUMNS = (
    "user_id", "card_template_id", "location", "position", "is_champion",
    "card_type", "template_guid", "card_state", "card_attributes",
    "card_abilities", "owner_user_id", "card_uses", "card_attack_mod",
    "card_defense_mod", "card_cost_mod", "card_damage", "permanent_buffs",
    "temporary_buffs", "original_template_guid", "resolved_at",
    "cost_mod_json", "temporary_attributes", "gems",
)


def _enabled(state: dict[str, Any] | None) -> bool:
    return bool((state or {}).get("trace_ability_resolution"))


def _cards(db, session_id: int) -> dict[int, tuple[Any, ...]]:
    from pvp_db import db_trace_card_rows
    rows = db_trace_card_rows(session_id, _CARD_COLUMNS, conn=db)
    return {int(row[0]): tuple(row[1:]) for row in rows}


def begin_effect(db, session, game, state: dict[str, Any], effect: dict,
                 target_uid: int | None):
    """Capture the pre-effect authoritative projection when tracing is on."""
    if not _enabled(state):
        return None
    return {
        "cards": _cards(db, session.session_id),
        "champions": _champions(state),
        "event_count": len(getattr(game, "events", []) or []),
        "effect_guid": str(effect.get("effect_guid") or "").lower(),
        "effect_type": str(effect.get("effect_type") or ""),
        "effect_order": int(effect.get("effect_order", 0) or 0),
        "target_uid": int(target_uid) if target_uid is not None else None,
    }


def end_effect(db, session, game, state: dict[str, Any], started,
               result: Any = None, error: Exception | None = None):
    """Append the exact card-row and event delta for one resolved leaf."""
    if started is None:
        return
    after = _cards(db, session.session_id)
    before = started.pop("cards")
    champions_before = started.pop("champions")
    changes = []
    for uid in sorted(set(before) | set(after)):
        old, new = before.get(uid), after.get(uid)
        if old == new:
            continue
        changes.append({
            "card_uid": uid,
            "before": _card_state(old),
            "after": _card_state(new),
        })
    events = getattr(game, "events", []) or []
    started["card_changes"] = changes
    started["champion_changes"] = _changes(
        champions_before, _champions(state))
    started["events"] = [_event(event)
                         for event in events[started.pop("event_count"):]]
    started["result"] = str(result or "")
    if error is not None:
        started["error"] = f"{type(error).__name__}: {error}"
    # Every value stored here is JSON-native.  This trace may safely survive a
    # choice/continuation boundary for a headless test to inspect later.
    state.setdefault("ability_trace", []).append(started)


def _card_state(value):
    if value is None:
        return None
    return dict(zip(_CARD_COLUMNS, value))


def _champions(state: dict[str, Any]) -> dict[str, Any]:
    """Snapshot all player/champion scalar maps in both PvE and PvP state."""
    keys = (
        "player_health", "ai_health", "player_charges", "ai_charges",
        "player_resources", "ai_resources", "player_total_resources",
        "ai_total_resources", "player_spell_points", "ai_spell_points",
        "player_threshold", "ai_threshold",
    )
    value = {key: state.get(key) for key in keys if key in state}
    for key, item in state.items():
        if key.startswith(("hp_", "charges_", "resources_",
                           "total_resources_", "spell_points_", "thresh_")):
            value[key] = item
    # Deep-copy and validate here so a trace cannot retain a mutable object or
    # hide a persistence bug behind a later in-place threshold update.
    return json.loads(json.dumps(value, sort_keys=True))


def _changes(before: dict[str, Any], after: dict[str, Any]):
    return {key: {"before": before.get(key), "after": after.get(key)}
            for key in sorted(set(before) | set(after))
            if before.get(key) != after.get(key)}


def _event(event) -> dict[str, Any]:
    """Keep event type plus JSON-safe public fields for wire assertions."""
    fields = {}
    for key, value in vars(event).items():
        if key.startswith("_"):
            continue
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            fields[key] = str(value)
        else:
            fields[key] = value
    return {"type": type(event).__name__, "fields": fields}
