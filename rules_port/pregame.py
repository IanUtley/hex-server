"""RulesPort-owned PreGame condition and setup-modifier helpers.

Data-driven from the extracted talent table and Records ability graphs; the
native resolver owns every authored effect.
"""

import json


def _as_dict(value):
    if isinstance(value, dict):
        return value
    if hasattr(value, "to_dict"):
        return value.to_dict()
    try:
        return dict(value)
    except (TypeError, ValueError):
        return {}


def _effect_list(db, ability_guid):
    """Return the effect specs for one ability from the Records graph."""
    from gamedata import DEFAULT_RECORD_STORE, ability_graph, runtime_effects
    graph = ability_graph(DEFAULT_RECORD_STORE, str(ability_guid).lower())
    if graph is None:
        raise RuntimeError(
            f"ability {str(ability_guid).lower()} is missing from current Records")
    return list(runtime_effects(graph))


def _effect_template_json(effect_guid):
    """Return one effect template as a plain dict from Records."""
    from gamedata import DEFAULT_RECORD_STORE
    return _as_dict(DEFAULT_RECORD_STORE.get(
        "AbilityEffectTemplate", str(effect_guid).lower()))



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

    The authored effect condition reads champion PermanentData. Map that
    predicate to the campaign's persisted consecutive-win streak, which is
    reset on a dungeon loss and incremented on each dungeon win.
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
    """Return ordered effect instances from the authoritative Records graph."""
    return _effect_list(db, ability_guid)


def _talent_record(talent_guid):
    """Return the typed ChampionTalentData record for a talent."""
    from gamedata import DEFAULT_RECORD_STORE
    return DEFAULT_RECORD_STORE.get(
        "ChampionTalentData", str(talent_guid or "").lower())


def _talent_field_int(talent_guid, field_name):
    record = _talent_record(talent_guid)
    if record is None:
        return 0
    try:
        return int(record.field(field_name, 0) or 0)
    except (AttributeError, TypeError, ValueError):
        return 0


def _ability_constants(ability_guid):
    """Return scalar defaults declared by typed AbilityConstant records."""
    from gamedata import DEFAULT_RECORD_STORE
    ability = DEFAULT_RECORD_STORE.get(
        "AbilityTemplate", str(ability_guid or "").lower())
    values = {}
    for variable in (ability.field("m_Variables", ()) if ability else ()) or ():
        item = _as_dict(variable)
        if str(item.get("_t") or "").rsplit(".", 1)[-1] != "AbilityConstant":
            continue
        name = item.get("m_Name")
        if not name:
            continue
        try:
            values[str(name)] = int(item.get("m_DefaultValue", 0) or 0)
        except (TypeError, ValueError):
            continue
    return values


def _typed_modifier_amount(ability_guid, modifier):
    """Resolve a static modifier operand from its typed field and constants."""
    from rules_port.bom_fields import field_variable_name, resolve_field
    modifier = _as_dict(modifier)
    constants = _ability_constants(ability_guid)
    value_field = modifier.get("m_ValueField")
    if value_field is None:
        value_field = modifier.get("m_InputValue")
    if value_field is not None:
        if isinstance(value_field, (bool, int, float)):
            return int(value_field)
        field = _as_dict(value_field)
        variable = field_variable_name(field)
        if variable:
            return constants.get(variable)
        kind = str(field.get("_t") or "").rsplit(".", 1)[-1]
        if kind in ("EffectOutputVariable", "OutputVariable",
                    "EffectCardIntegerVariable", "CardIntegerVariable"):
            return None
        try:
            return int(resolve_field(field, variables=constants, default=0))
        except (TypeError, ValueError):
            return None
    for name in ("m_Value", "m_Amount_DEPRECATED", "m_Amount"):
        value = modifier.get(name)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def _effect_params(db, ability_guid):
    """Return typed modifier fields paired with their effect/target metadata."""
    from rules_port.bom_fields import modifier_metadata
    graph_effects = _effect_rows(db, ability_guid)
    graph = None
    try:
        from gamedata import DEFAULT_RECORD_STORE, ability_graph
        graph = ability_graph(DEFAULT_RECORD_STORE, str(ability_guid).lower())
    except (AttributeError, TypeError, ValueError):
        pass
    for effect in graph_effects:
        if effect.get("effect_type") != "CardModifierAbilityEffectTemplate":
            continue
        effect_guid = effect.get("effect_guid")
        template = _effect_template_json(effect_guid) or {}
        modifier = _as_dict(template.get("m_Modifier"))
        if not modifier:
            continue
        param = modifier_metadata(effect_guid)
        param["amount"] = _typed_modifier_amount(ability_guid, modifier)
        param["condition_id"] = str(effect.get("condition_id") or "")
        param["target_index"] = int(effect.get("target_index", -1))
        if graph is not None and 0 <= param["target_index"] < len(graph.targets):
            target = graph.targets[param["target_index"]]
            card_types, zones = _target_filter_flags(target.card_filter)
            param["target_is_random"] = bool(target.is_random)
            param["target_card_types"] = card_types
            param["target_zones"] = zones
        yield param


def ability_starting_hand_size_modifier(db, ability_guid):
    """Resolve a typed starting-hand-size delta on one ability graph."""
    total = 0
    for param in _effect_params(db, ability_guid):
        if (str(param.get("property") or "").lower() != "intattr" or
                str(param.get("attribute") or "").rsplit(".", 1)[-1] !=
                "StartingHandSizeModifiers" or
                str(param.get("operation") or "Add") != "Add" or
                not _effect_condition_allows_fallback(
                    db, None, param.get("condition_id"))):
            continue
        amount = param.get("amount")
        if amount is not None:
            total += int(amount)
    return total


def _target_filter_flags(value):
    """Return the typed card-type and exact-zone predicates in a filter."""
    card_types, zones = set(), set()

    def visit(node):
        node = _as_dict(node)
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


def _condition_contains_previous_dungeon_win(condition):
    """Recognize the typed condition for a prior dungeon victory."""
    condition = _as_dict(condition)
    if str(condition.get("_t") or "").rsplit(".", 1)[-1] != \
            "RequiresCardsControlled":
        return False
    if "Champions" not in str(condition.get("m_CardCollection") or "").split("|"):
        return False
    predicates = {}

    def visit(node):
        node = _as_dict(node)
        if isinstance(node, dict):
            if str(node.get("_t") or "").rsplit(".", 1)[-1] == "IntAttrFilter":
                name = str(node.get("m_Attribute") or "").rsplit(">", 1)[-1]
                try:
                    value = int(node.get("m_Value", 0) or 0)
                except (TypeError, ValueError):
                    value = None
                predicates[name] = (node.get("m_ComparisonOp"), value)
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(condition.get("m_CardFilter"))
    wins = predicates.get("DungeonWins")
    losses = predicates.get("DungeonConseqLosses")
    return bool(wins and wins[0] == "GreaterThanOrEqual" and
                wins[1] is not None and wins[1] >= 1 and losses and
                losses[0] == "Equals" and losses[1] == 0)


def _effect_condition_allows_fallback(db, session, condition_id):
    """Evaluate effect conditions that need the campaign-state adapter.

    The authored dungeon-win predicate is a typed Records condition, but this
    setup helper has no champion object to project its PermanentData path
    through. Map that predicate to the campaign's persisted win streak.
    Unknown conditions fail closed here; the native RulesPort resolver owns
    their gameplay evaluation.
    """
    condition_id = str(condition_id or "").lower()
    if not condition_id or condition_id == "0" * 36:
        return True
    from gamedata import DEFAULT_RECORD_STORE
    record = DEFAULT_RECORD_STORE.get(
        "AbilityEffectConditionTemplate", condition_id)
    condition = _as_dict(record.field("m_Condition")) if record else {}
    if _condition_contains_previous_dungeon_win(condition):
        return _has_previous_dungeon_win(db, session)
    return False


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
    from pvp_db import db_talent_ability_condition
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

        result["health"] += _talent_field_int(
            talent_guid, "m_StartingHealthModifier")

        effect_params = list(_effect_params(db, str(guid)))
        charge_total = 0
        rage_by_target = {}
        graph = None
        try:
            from gamedata import DEFAULT_RECORD_STORE, ability_graph
            graph = ability_graph(DEFAULT_RECORD_STORE, str(guid).lower())
        except (AttributeError, TypeError, ValueError):
            pass
        for param in effect_params:
            if not _effect_condition_allows_fallback(
                    db, session, param.get("condition_id")):
                continue
            prop = (param.get("property") or "").lower()
            if prop == "chargepoints":
                amount = param.get("amount")
                if amount is not None:
                    charge_total += int(amount)
            elif prop == "intattr":
                attr = str(param.get("attribute") or "").rsplit(".", 1)[-1]
                amount = param.get("amount")
                if amount is None:
                    continue
                amount = int(amount)
                if attr == "StartingHandSizeModifiers":
                    result["starting_hand"] += amount
                elif attr == "MaximumHandSizeModifiers":
                    result["maximum_hand"] += amount
                elif (attr.lower() == "rage" and amount > 0 and graph and
                      graph.trigger_event_type.rsplit(".", 1)[-1] ==
                      "GameStartedEvent" and param.get("target_is_random") and
                      "Troop" in (param.get("target_card_types") or set()) and
                      (param.get("target_zones") or set()) == {"Hand"}):
                    target_index = int(param.get("target_index", -1))
                    rage_by_target[target_index] = max(
                        rage_by_target.get(target_index, 0), amount)
        result["charges"] += charge_total

        if graph is None:
            continue
        for target_index, rage in sorted(rage_by_target.items()):
            target = graph.targets[target_index]
            card_types, _zones = _target_filter_flags(target.card_filter)
            result["starting_hand_effects"].append({
                "ability_guid": str(guid).lower(),
                "rage": rage,
                "card_types": sorted(card_types),
            })

        # Starting-hand cost modifiers (e.g. Devoted) are authored as
        # GameStartedEvent triggered abilities.  They are discovered and
        # resolved by the normal trigger dispatcher, so applying their
        # ``cardcost`` leaf here as well would reduce the cost twice.  The
        # target metadata identifies any setup-only hand mutations that need
        # the campaign opening-hand adapter.
    return result


def passive_talent_starting_health_modifier(db, talent_guids):
    """Return starting-health modifiers from passive talent metadata.

    Passive talent records expose this as ``m_StartingHealthModifier``.
    Ignore talents with ability rows here so their modifier is not counted
    twice by :func:`pregame_modifiers`.
    """
    total = 0
    for talent_guid in talent_guids or []:
        record = _talent_record(talent_guid)
        if record is None:
            continue
        if record.field("m_Abilities", ()):
            continue
        total += _talent_field_int(talent_guid, "m_StartingHealthModifier")
    return total


def _apply_bom_health(db, ability_guid):
    """Health a PreGame ability grants: the sum of its CardModifier leaves that
    actually gain champion health (property healhero), e.g. Shard Attuned's
    "gain 1 health".  Attribute/damage/stat leaves are NOT health gains."""
    total = 0
    for param in _effect_params(db, ability_guid):
        if str(param.get("property") or "").lower() != "healhero":
            continue
        amount = param.get("amount")
        if amount is not None:
            total += max(0, int(amount))
    return total


def apply_pregame_abilities(game, session, db, handler, player_uid, user_id,
                            ability_guids, health_field,
                            metadata_only=False):
    """Apply champion setup modifiers and PreGame deck insertion effects.

    Starting-health and charge values come from typed talent/effect records.
    Authored deck insertions are resolved from their ability graphs before the
    opening hand is dealt.
    """
    import game_engine
    from rules_port.pregame import _effect_list, _effect_template_json
    effect_template = _effect_template_json
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
            from rules_port.resolution import resolve_port_ability
            result = resolve_port_ability(
                handler, game, session, db, pl_t, ai_t, bstate,
                str(ability_guid).lower(), source_uid,
                int(user_id or 0), target_map={})
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
