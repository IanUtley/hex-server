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
import re

_CONDITIONS = {}

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


def register_condition(name):
    def deco(fn):
        _CONDITIONS[name] = fn
        return fn
    return deco


@register_condition("pregame_shards_in_deck")
def _cond_shards_in_deck(db, session, user_id, color, count):
    row = db.execute(
        "SELECT COUNT(*) FROM game_cards gc "
        "JOIN card_templates ct ON ct.guid = gc.template_guid "
        "WHERE gc.session_id=? AND gc.user_id=? AND gc.location='deck' "
        "AND ct.name LIKE ?",
        (session.session_id, user_id, f"%{color} Shard%")).fetchone()
    return (row[0] if row else 0) >= int(count)


@register_condition("pregame_cards_in_deck")
def _cond_cards_in_deck(db, session, user_id, count):
    row = db.execute(
        "SELECT COUNT(*) FROM game_cards "
        "WHERE session_id=? AND user_id=? AND location='deck'",
        (session.session_id, user_id)).fetchone()
    return (row[0] if row else 0) >= int(count)


@register_condition("pregame_is_dungeon")
def _cond_is_dungeon(db, session, user_id):
    # Campaign battles use the campaign ruleset, not the dungeon encounter
    # ruleset.  In particular, Fearless is a dungeon-boss hand-size effect
    # and must not change the Orc Warrior campaign opening hand.
    return not (session and (session.session_name or "").startswith("camp_"))


def _has_previous_dungeon_win(db, session):
    """Return whether this dungeon has a completed encounter win already.

    Ruthlessly Efficient is a dungeon-run bonus, not a general dungeon
    opening bonus.  The extracted ability row lost its nested condition, so
    use the campaign's persisted win count to recover the intended timing.
    """
    session_name = (session.session_name or "") if session else ""
    if not session_name.startswith("camp_"):
        return False
    try:
        camp_id = int(session_name[5:].split("_", 1)[0])
    except (TypeError, ValueError):
        return False
    row = db.execute(
        "SELECT campaign_type, state_json FROM campaigns WHERE id=?",
        (camp_id,)).fetchone()
    if not row or (row[0] or "").upper() != "DUNGEON":
        return False
    try:
        state = json.loads(row[1] or "{}")
    except (TypeError, ValueError):
        return False
    try:
        return int(state.get("Wins", 0) or 0) > 0
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


def _effect_params(db, ability_guid):
    """Return decoded CardModifier parameters for an ability's direct BOM."""
    rows = db.execute(
        "SELECT effect_type, param FROM ability_effects "
        "WHERE ability_guid=? ORDER BY effect_order",
        (ability_guid,)).fetchall()
    for effect_type, raw_param in rows:
        if effect_type != "CardModifierAbilityEffectTemplate":
            continue
        try:
            param = json.loads(raw_param or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(param, dict):
            yield param


def _target_template_flags(db, ability_guid):
    """Return card-type and exact-zone filters from a talent target."""
    row = db.execute(
        "SELECT target_template_ids FROM talent_abilities WHERE ability_guid=? "
        "LIMIT 1", (ability_guid,)).fetchone()
    if not row:
        return set(), set()
    try:
        target_guids = json.loads(row[0] or "[]")
    except (TypeError, ValueError):
        target_guids = []
    if not isinstance(target_guids, list):
        return set(), set()

    card_types, zones = set(), set()

    def visit(value):
        if isinstance(value, dict):
            template_type = str(value.get("_t") or "")
            if template_type.endswith("IsType"):
                card_types.update(part for part in str(
                    value.get("m_CardType") or "").split("|") if part)
            elif template_type.endswith("InZone"):
                collection = value.get("m_Collection")
                if collection:
                    zones.add(str(collection))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for target_guid in target_guids:
        target = db.execute(
            "SELECT filter_json FROM target_templates WHERE template_id=?",
            (str(target_guid),)).fetchone()
        if not target:
            continue
        try:
            visit(json.loads(target[0] or "{}"))
        except (TypeError, ValueError):
            continue
    return card_types, zones


def _card_cost_delta(param):
    """Resolve card-cost deltas, including older BOM rows with amount=0."""
    try:
        amount = int(param.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0
    if amount:
        return amount
    match = re.search(r"\bcost\s*([+-])\s*\[?\(?\s*(\d+)",
                      param.get("text") or "", re.IGNORECASE)
    if not match:
        return 0
    value = int(match.group(2))
    return -value if match.group(1) == "-" else value


def _text_number(text, expression):
    match = re.search(expression, text or "", re.IGNORECASE)
    if not match:
        return 0
    value = match.group(1)
    try:
        return int(value)
    except ValueError:
        return _NUMBER_WORDS.get(value.lower(), 0)


def pregame_modifiers(db, session, user_id, ability_guids):
    """Resolve the starting-game modifiers encoded by granted talent BOMs.

    The extracted talent table contains the ability GUID and its trigger
    condition, while the BOM parameters contain the actual operation.  This
    keeps the campaign setup independent of individual talent names/cards.
    """
    result = {
        "health": 0,
        "charges": 0,
        "starting_hand": 0,
        "maximum_hand": 0,
        "starting_hand_effects": [],
    }
    for guid in ability_guids or []:
        row = db.execute(
            "SELECT talent_guid, condition FROM talent_abilities "
            "WHERE ability_guid=? LIMIT 1", (str(guid),)).fetchone()
        if not row:
            # Signature champion powers are not PreGame talent abilities.
            continue
        talent_guid, condition = row
        condition = condition or ""
        if condition and not evaluate_condition(condition, db, session, user_id):
            continue

        talent = db.execute(
            "SELECT description FROM talent_data WHERE talent_guid=?",
            (talent_guid,)).fetchone()
        description = talent[0] if talent else ""
        # Ruthlessly Efficient is authored as a dungeon-only pre-game ability,
        # but older extracted rows have no condition field. Its bonus starts
        # only after a previous win in this same dungeon.
        if (re.match(r"\s*dungeons\s*:", description or "", re.IGNORECASE)
                and not _has_previous_dungeon_win(db, session)):
            continue
        health = re.search(r"([+-]?\d+)\s+starting health", description or "",
                           re.IGNORECASE)
        if health:
            result["health"] += int(health.group(1))

        effect_params = list(_effect_params(db, str(guid)))
        for param in effect_params:
            prop = (param.get("property") or "").lower()
            text = param.get("text") or ""
            if prop == "chargepoints":
                amount = int(param.get("amount") or 0)
                if not amount:
                    amount = _text_number(text, r"gain\s+([a-z]+|\d+)\s+charges?")
                result["charges"] += amount
            elif prop == "intattr":
                result["starting_hand"] += _text_number(
                    text, r"starting hand size is increased by\s+([a-z]+|\d+)")
                result["maximum_hand"] += _text_number(
                    text, r"maximum hand size is increased by\s+([a-z]+|\d+)")

        if re.search(r"random troop in your starting hand", description or "",
                     re.IGNORECASE):
            if any("rage" in (param.get("text") or "").lower()
                   for param in effect_params):
                result["starting_hand_effects"].append({
                    "ability_guid": str(guid).lower(),
                    "rage": max(
                        _text_number(param.get("text") or "",
                                     r"rage\s+([a-z]+|\d+)")
                        for param in effect_params
                        if "rage" in (param.get("text") or "").lower()),
                })

        target_types, target_zones = _target_template_flags(db, str(guid))
        if (target_zones and "Hand" in target_zones
                and target_types & {"BasicAction", "QuickAction"}
                and re.search(r"starting hand", description or "",
                              re.IGNORECASE)):
            for param in effect_params:
                if ((param.get("property") or "").lower() != "cardcost"
                        or str(param.get("duration") or "").lower()
                        != "permanent"):
                    continue
                delta = _card_cost_delta(param)
                if delta:
                    result["starting_hand_effects"].append({
                        "ability_guid": str(guid).lower(),
                        "card_cost_mod": delta,
                        "card_types": sorted(target_types &
                                              {"BasicAction", "QuickAction"}),
                    })
                    break
    return result


def passive_talent_starting_health_modifier(db, talent_guids):
    """Return starting-health modifiers from passive talent metadata.

    Most PreGame talent effects are represented by a talent ability and are
    resolved through :func:`pregame_modifiers`.  Passive talents such as
    Weight have no ability row, so their signed ``starting health`` text is
    the only extracted modifier available to battle setup.  Ignore talents
    that do have an ability row here so their modifier is not counted twice.
    """
    total = 0
    for talent_guid in talent_guids or []:
        row = db.execute(
            "SELECT description FROM talent_data WHERE talent_guid=?",
            (str(talent_guid),)).fetchone()
        if not row:
            continue
        if db.execute(
                "SELECT 1 FROM talent_abilities WHERE talent_guid=? LIMIT 1",
                (str(talent_guid),)).fetchone():
            continue
        for match in re.finditer(
                r"([+-]?\d+)\s+starting health\b", row[0] or "",
                re.IGNORECASE):
            total += int(match.group(1))
    return total


def _apply_bom_health(db, ability_guid):
    """Health a PreGame ability grants: the sum of its CardModifier leaves that
    actually gain champion health (property healhero), e.g. Shard Attuned's
    "gain 1 health".  Attribute/damage/stat leaves are NOT health gains."""
    import json as _json
    import re as _re
    rows = db.execute(
        "SELECT effect_guid, effect_type, param FROM ability_effects "
        "WHERE ability_guid=? ORDER BY effect_order",
        (ability_guid,)).fetchall()
    total = 0
    for _eg, et, param in rows:
        if et != "CardModifierAbilityEffectTemplate":
            continue
        try:
            p = _json.loads(param or "{}")
        except Exception:
            p = {}
        if p.get("property") == "healhero":
            amt = int(p.get("amount") or 0)
            if not amt:
                m = _re.search(r'gain\s+(\d+)\s+health',
                               (p.get("text") or "").lower())
                amt = int(m.group(1)) if m else 1
            total += max(0, amt)
    return total


def apply_pregame_abilities(game, session, db, handler, player_uid, user_id, ability_guids, health_field):
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
    new_charges = old_charges + modifiers["charges"]
    if new_charges != old_charges:
        game.__dict__[charge_field] = new_charges
        ev = game_engine.ChampionChargePointsChangedSessionEventArgs()
        ev.player_id = player_uid
        ev.operation = 1 if modifiers["charges"] >= 0 else 2
        ev.delta = modifiers["charges"]
        ev.new_value = new_charges
        game._push(ev)

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
        row = db.execute(
            "SELECT COUNT(*) FROM game_cards WHERE session_id=? "
            "AND user_id=? AND location='deck'",
            (session.session_id, user_id)).fetchone()
        counts[str(user_id)] = int(row[0] if row else 0)

    # The current seed stores the complete parent-level effect metadata, so
    # selecting these by effect type covers every authored PreGame token/deck
    # grant without individual champion/card-name rules.
    selected = {str(guid).lower() for guid in (ability_guids or [])}
    rows = []
    if selected:
        placeholders = ",".join("?" * len(selected))
        rows = db.execute(
            "SELECT DISTINCT ability_guid, condition FROM talent_abilities "
            "WHERE ability_guid IN ({}) AND (activatable_phases & 4) != 0"
            .format(placeholders), tuple(selected)).fetchall()
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
            result = resolve_ability(
                handler, game, session, db, pl_t, ai_t, bstate,
                str(ability_guid).lower(), source_uid, int(user_id or 0), {})
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
