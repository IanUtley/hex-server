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

    Ruthlessly Efficient is a dungeon-run bonus, not a general dungeon
    opening bonus.  The extracted ability row lost its nested condition, so
    use the campaign's persisted consecutive-win streak to recover the
    intended timing.  The streak is reset to 0 on a dungeon loss and
    incremented on each dungeon win (see ``campaign._apply_gameend``).
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


def _effect_rows(db, ability_guid):
    """Return direct BOM rows for an ability in authored effect order."""
    from pvp_db import db_ability_effect_type_params, db_ability_effect_rows
    rows = db_ability_effect_rows(ability_guid, conn=db)
    ordered = db_ability_effect_type_params(ability_guid, conn=db)
    by_type = {}
    for effect_guid, effect_type, param in rows:
        by_type.setdefault((effect_type, param), effect_guid)
    return [(by_type.get((effect_type, param)), effect_type, param)
            for effect_type, param in ordered]


def _effect_params(db, ability_guid):
    """Return decoded CardModifier parameters for an ability's direct BOM."""
    rows = _effect_rows(db, ability_guid)
    for _effect_guid, effect_type, raw_param in rows:
        if effect_type != "CardModifierAbilityEffectTemplate":
            continue
        try:
            param = json.loads(raw_param or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(param, dict):
            yield param


def _target_filter_flags(value):
    """Return the typed card-type and exact-zone predicates in a filter."""
    card_types, zones = set(), set()

    def visit(node):
        if isinstance(node, dict):
            template_type = str(node.get("_t") or "")
            if template_type.endswith("IsType"):
                card_types.update(part for part in str(
                    node.get("m_CardType") or "").split("|") if part)
            elif template_type.endswith("IsTroop"):
                card_types.add("Troop")
            elif template_type.endswith("InZone"):
                collection = node.get("m_Collection")
                if collection:
                    zones.add(str(collection))
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return card_types, zones


def _target_template_flags_for_id(db, target_id):
    from pvp_db import db_target_template_filter
    value = db_target_template_filter(str(target_id), conn=db)
    if not value:
        return set(), set()
    try:
        value = json.loads(value or "{}")
    except (TypeError, ValueError):
        return set(), set()
    return _target_filter_flags(value)


def _effect_target_flags(db, ability_guid, effect_guid, effect_order):
    """Return the target filter attached to one direct BOM effect.

    Talent ability rows historically did not store parent effect wiring in
    SQLite.  Read the target index from the authoritative Records graph first
    so existing databases still resolve a target index of zero correctly.
    """
    from pvp_db import (db_talent_target_template_ids,
                        db_ability_effect_target_index)
    payload = db_talent_target_template_ids(ability_guid, conn=db)
    if not payload:
        return set(), set()
    try:
        target_ids = json.loads(payload or "[]")
    except (TypeError, ValueError):
        target_ids = []
    if not isinstance(target_ids, list):
        return set(), set()

    target_index = None
    try:
        from gamedata import DEFAULT_RECORD_STORE, ability_graph, runtime_effects
        graph = ability_graph(DEFAULT_RECORD_STORE, str(ability_guid).lower())
        effects = runtime_effects(graph) if graph else []
        for index, effect in enumerate(effects):
            if (index == int(effect_order) or
                    str(effect.get("effect_guid") or "").lower()
                    == str(effect_guid).lower()):
                target_index = int(effect.get("target_index", -1))
                break
    except (AttributeError, TypeError, ValueError, RuntimeError):
        target_index = None
    if target_index is None:
        db_row = db_ability_effect_target_index(
            ability_guid, effect_guid, conn=db)
        if db_row is not None:
            try:
                target_index = int(db_row)
            except (TypeError, ValueError):
                target_index = -1
    if target_index is None or target_index < 0 or target_index >= len(target_ids):
        return set(), set()
    return _target_template_flags_for_id(db, target_ids[target_index])


def _legacy_target_template_flags(db, ability_guid):
    """Return aggregate target flags for older callers."""
    from pvp_db import db_talent_target_template_ids
    payload = db_talent_target_template_ids(ability_guid, conn=db)
    if not payload:
        return set(), set()
    try:
        target_guids = json.loads(payload or "[]")
    except (TypeError, ValueError):
        target_guids = []
    card_types, zones = set(), set()
    for target_guid in target_guids if isinstance(target_guids, list) else []:
        types, target_zones = _target_template_flags_for_id(db, target_guid)
        card_types.update(types)
        zones.update(target_zones)
    return card_types, zones


def _target_template_flags(db, ability_guid):
    """Return aggregate card-type and exact-zone filters for a talent."""
    return _legacy_target_template_flags(db, ability_guid)


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


def pregame_modifiers(db, session, user_id, ability_guids,
                      include_triggered=True):
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
    from pvp_db import db_talent_ability_condition, db_talent_description
    for guid in ability_guids or []:
        # Attached RulesPort sessions dispatch abilities with an authored
        # trigger through the native event path.  Callers doing the small
        # metadata fallback for those sessions must not count such an ability
        # a second time (for example a PreGame heal BOM).
        if not include_triggered:
            try:
                from gamedata import DEFAULT_RECORD_STORE, ability_graph
                graph = ability_graph(DEFAULT_RECORD_STORE, str(guid).lower())
                if graph is not None and graph.trigger_event_type:
                    continue
            except Exception:
                pass
        row = db_talent_ability_condition(str(guid), conn=db)
        if not row:
            # Signature champion powers are not PreGame talent abilities.
            continue
        talent_guid, condition = row
        condition = condition or ""
        if condition and not evaluate_condition(condition, db, session, user_id):
            continue

        description = db_talent_description(talent_guid, conn=db) or ""
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
        charge_total = 0
        for param in effect_params:
            prop = (param.get("property") or "").lower()
            text = param.get("text") or ""
            if prop == "chargepoints":
                amount = int(param.get("amount") or 0)
                if not amount:
                    amount = _text_number(text, r"gain\s+([a-z]+|\d+)\s+charges?")
                charge_total += amount
            elif prop == "intattr":
                # IntAttrModifier.m_Attribute/m_Value are the rules fields;
                # game text is localized presentation and may be absent.
                attr = str(param.get("attribute") or "").rsplit(".", 1)[-1]
                amount = int(param.get("amount") or 0)
                if attr == "StartingHandSizeModifiers":
                    result["starting_hand"] += amount
                elif attr == "MaximumHandSizeModifiers":
                    result["maximum_hand"] += amount
                else:
                    result["starting_hand"] += _text_number(
                        text, r"starting hand size is increased by\s+([a-z]+|\d+)")
                    result["maximum_hand"] += _text_number(
                        text, r"maximum hand size is increased by\s+([a-z]+|\d+)")

        if charge_total == 0:
            charge_total = _text_number(
                description, r"(?:begin|start).*?with\s+([a-z]+|\d+)\s+charges?")
        result["charges"] += charge_total

        if re.search(r"random troop in your starting hand", description or "",
                     re.IGNORECASE):
            rage_params = [param for param in effect_params
                           if str(param.get("attribute") or "").rsplit(
                               ".", 1)[-1].lower() == "rage"]
            if not rage_params:
                rage_params = [param for param in effect_params
                               if "rage" in (param.get("text") or "").lower()]
            if rage_params:
                result["starting_hand_effects"].append({
                    "ability_guid": str(guid).lower(),
                    "rage": max(
                        int(param.get("amount") or _text_number(
                            param.get("text") or "", r"rage\s+([a-z]+|\d+)"))
                        for param in rage_params),
                })

        # Starting-hand cost modifiers (e.g. Devoted) are authored as
        # GameStartedEvent triggered abilities.  They are discovered and
        # resolved by the normal trigger dispatcher, so applying their
        # ``cardcost`` leaf here as well would reduce the cost twice.  The
        # rage variant below stays because its trigger leaf is an ``intattr``
        # with no attribute and therefore does not apply natively.
    return result


def passive_talent_starting_health_modifier(db, talent_guids):
    """Return starting-health modifiers from passive talent metadata.

    Most PreGame talent effects are represented by a talent ability and are
    resolved through :func:`pregame_modifiers`.  Passive talents such as
    Weight have no ability row, so their signed ``starting health`` text is
    the only extracted modifier available to battle setup.  Ignore talents
    that do have an ability row here so their modifier is not counted twice.
    """
    from pvp_db import db_talent_description, db_talent_has_ability
    total = 0
    for talent_guid in talent_guids or []:
        description = db_talent_description(talent_guid, conn=db)
        if not description:
            continue
        if db_talent_has_ability(talent_guid, conn=db):
            continue
        for match in re.finditer(
                r"([+-]?\d+)\s+starting health\b", description or "",
                re.IGNORECASE):
            total += int(match.group(1))
    return total


def _apply_bom_health(db, ability_guid):
    """Health a PreGame ability grants: the sum of its CardModifier leaves that
    actually gain champion health (property healhero), e.g. Shard Attuned's
    "gain 1 health".  Attribute/damage/stat leaves are NOT health gains."""
    import json as _json
    import re as _re
    from pvp_db import db_ability_effect_rows
    rows = db_ability_effect_rows(ability_guid, conn=db)
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
