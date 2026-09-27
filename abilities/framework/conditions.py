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
    """Apply PreGame-triggered champion abilities for a player.

    For each granted ability marked PreGame in talent_abilities, evaluate its
    ``condition`` spec; if it holds, apply the ability's BOM health gains and
    data-driven deck-insertion effects. Deck insertions are resolved before
    the opening hand is dealt.
    """
    import game_engine
    from .fields import effect_template
    from .resolution import _effect_list, resolve_ability
    modifiers = pregame_modifiers(db, session, user_id, ability_guids)
    old_health = game.__dict__.get(health_field, 20)
    new_health = old_health + modifiers["health"]
    if new_health != old_health:
        game.__dict__[health_field] = new_health
        ev = game_engine.ChampionHealthChangedSessionEventArgs()
        ev.player_id = player_uid
        ev.old_damage_value = old_health
        ev.new_damage_value = new_health
        game._push(ev)

    charge_field = "player_charges" if health_field == "player_health" else "ai_charges"
    old_charges = int(game.__dict__.get(charge_field, 0) or 0)
    # In an attached session native dispatch may already have applied a
    # chargepoint leaf.  Starting charges begin at zero, so only fill the
    # metadata amount still missing from the current pool.
    charge_delta = modifiers["charges"]
    if metadata_only and charge_delta > 0:
        charge_delta = max(0, charge_delta - old_charges)
    new_charges = old_charges + charge_delta
    if new_charges != old_charges:
        game.__dict__[charge_field] = new_charges
        ev = game_engine.ChampionChargePointsChangedSessionEventArgs()
        ev.player_id = player_uid
        ev.operation = 1 if charge_delta >= 0 else 2
        ev.delta = charge_delta
        ev.new_value = new_charges
        game._push(ev)

    # Native dispatch owns authored trigger/BOM effects in attached sessions.
    # The metadata-only pass above exists for abilities such as Fury whose
    # starting modifiers are present on the talent but have no trigger event.
    if metadata_only:
        return

    # The normal battle state is created after mulligan, but the BOM resolver
    # needs the same owner/target context while it creates cards during setup.
    bstate = game.__dict__.setdefault("_pregame_bstate", {
        "event_type": "PreGameEvent",
        "session_id": session.session_id,
        "player_health": int(game.__dict__.get("player_health", 20) or 0),
        "ai_health": int(game.__dict__.get("ai_health", 20) or 0),
        "player_charges": int(game.__dict__.get("player_charges", 0) or 0),
        "ai_charges": int(game.__dict__.get("ai_charges", 0) or 0),
    })
    bstate[health_field] = int(game.__dict__.get(health_field, 20) or 0)
    bstate[charge_field] = int(game.__dict__.get(charge_field, 0) or 0)
    counts = bstate.setdefault("pregame_initial_deck_counts", {})
    if str(user_id) not in counts:
        from pvp_db import db_deck_card_count
        counts[str(user_id)] = db_deck_card_count(
            session.session_id, user_id, conn=db)

    # The current seed stores the complete parent-level effect metadata, so
    # selecting these by effect type covers every authored PreGame token/deck
    # grant without individual champion/card-name rules.
    selected = {str(guid).lower() for guid in (ability_guids or [])}
    from pvp_db import db_pregame_talent_rows
    rows = db_pregame_talent_rows(selected, conn=db)
    source_attr = "_player_champ_scid" if user_id else "_ai_champ_scid"
    source_scid = getattr(handler, source_attr, None)
    source_uid = (int(source_scid.uid.uid64) if source_scid is not None
                  else int(player_uid))
    pl_t = getattr(game, "player_uid", None)
    ai_t = getattr(game, "ai_uid", None)
    logs = []
    for ability_guid, condition in rows:
        if condition and not evaluate_condition(condition, db, session, user_id):
            continue
        effects = _effect_list(db, ability_guid)
        deck_effect = False
        for effect in effects:
            if effect.get("effect_type") != "SummonTokenTroopAbilityEffectTemplate":
                continue
            template = effect_template(effect.get("effect_guid")) or {}
            if str(template.get("m_CardCollection") or "").lower() == "deck":
                deck_effect = True
                break
        if not deck_effect:
            continue
        try:
            if getattr(session, "_rules_port_session", None) is not None:
                from rules_port.resolution import resolve_port_ability
                result = resolve_port_ability(
                    handler, game, session, db, pl_t, ai_t, bstate,
                    str(ability_guid).lower(), source_uid,
                    int(user_id or 0), target_map={})
            else:
                result = resolve_ability(
                    handler, game, session, db, pl_t, ai_t, bstate,
                    str(ability_guid).lower(), source_uid,
                    int(user_id or 0), {})
            if result:
                logs.append(f"{ability_guid}: {result}")
        except Exception as exc:
            # Keep the existing health/charge setup usable if one optional
            # authored BOM cannot be resolved.
            logs.append(f"{ability_guid}: error {exc}")

    summary = (f"PreGame: health {old_health}->{new_health}, "
               f"charges {old_charges}->{new_charges}")
    if logs:
        summary += "; " + "; ".join(logs)
    return summary
