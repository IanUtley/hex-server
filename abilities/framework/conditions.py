"""PreGame condition functions for champion talent abilities.

Each condition spec (generated from gamedata m_TriggerCondition) names a Python
function plus args.  The PreGame pass evaluates the function; the ability's BOM
effects are applied only if it returns True.

Supported specs:
    pregame_shards_in_deck:COLOR,COUNT   — COUNT+ cards of COLOR in deck
    pregame_cards_in_deck:COUNT          — COUNT+ cards in deck (any type)
    pregame_is_dungeon                   — running a dungeon encounter
"""

import json

_CONDITIONS = {}

def register_condition(name):
    def deco(fn):
        _CONDITIONS[name] = fn
        return fn
    return deco


@register_condition("pregame_shards_in_deck")
def _cond_shards_in_deck(db, session, user_id, color, count):
    from pvp_db import db_deck_shard_count
    return db_deck_shard_count(session.session_id, user_id, color, conn=db) >= int(count)


@register_condition("pregame_cards_in_deck")
def _cond_cards_in_deck(db, session, user_id, count):
    from pvp_db import db_deck_card_count
    return db_deck_card_count(session.session_id, user_id, conn=db) >= int(count)


@register_condition("pregame_is_dungeon")
def _cond_is_dungeon(db, session, user_id):
    # Campaign battles use the campaign ruleset, not the dungeon encounter
    # ruleset.  In particular, Fearless is a dungeon-boss hand-size effect
    # and must not change the Orc Warrior campaign opening hand.
    return not (session and (session.session_name or "").startswith("camp_"))


def _has_previous_dungeon_win(db, session):
    """Return whether the previous encounter in this dungeon was won.

    Setup condition evaluation maps the authored PermanentData predicates to
    the campaign's persisted consecutive-win streak. The streak is reset to 0
    on a dungeon loss and incremented on each dungeon win.
    """
    session_name = (session.session_name or "") if session else ""
    if not session_name.startswith("camp_"):
        return False
    try:
        camp_id = int(session_name[5:].split("_", 1)[0])
    except (TypeError, ValueError):
        return False
    from pve_db import db_campaign_runtime_row
    # ``db_campaign_runtime_row`` returns ``(state_json, campaign_type)``.
    row = db_campaign_runtime_row(camp_id, conn=db)
    if not row or (row[1] or "").upper() != "DUNGEON":
        return False
    try:
        state = json.loads(row[0] or "{}")
    except (TypeError, ValueError):
        return False
    try:
        return int(state.get("dungeon_win_count", 0) or 0) > 0
    except (TypeError, ValueError):
        return False


def evaluate_condition(condition, db, session, user_id):
    """Run a condition spec ('' = unconditional True). Returns bool."""
    if not condition:
        return True
    name, _, args = condition.partition(":")
    fn = _CONDITIONS.get(name)
    if not fn:
        return True
    try:
        fn_args = args.split(",") if args else []
        return bool(fn(db, session, user_id, *fn_args))
    except (TypeError, ValueError):
        return True


def pregame_modifiers(db, session, user_id, ability_guids,
                      include_triggered=True):
    """Compatibility wrapper for the Records-backed RulesPort setup helper."""
    from rules_port.pregame import pregame_modifiers as resolve
    return resolve(db, session, user_id, ability_guids,
                   include_triggered=include_triggered)


def passive_talent_starting_health_modifier(db, talent_guids):
    """Compatibility wrapper for typed ChampionTalentData health metadata."""
    from rules_port.pregame import \
        passive_talent_starting_health_modifier as resolve
    return resolve(db, talent_guids)


def _apply_bom_health(db, ability_guid):
    """Compatibility wrapper for typed HealHero modifier interpretation."""
    from rules_port.pregame import _apply_bom_health as resolve
    return resolve(db, ability_guid)


def apply_pregame_abilities(game, session, db, handler, player_uid, user_id,
                            ability_guids, health_field,
                            metadata_only=False):
    """Compatibility entry point; all setup resolution belongs to RulesPort."""
    from rules_port.pregame import apply_pregame_abilities as resolve
    return resolve(
        game, session, db, handler, player_uid, user_id, ability_guids,
        health_field, metadata_only=metadata_only)
