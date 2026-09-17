"""RulesPort-owned target candidate and selection evaluation.

Target legality is a rules decision, so live sessions must not call the
historical ``abilities.framework.targeting`` implementation.  This module
keeps the database projection at the edge and evaluates the extracted
Records filter through the native filter port.
"""

from __future__ import annotations

import json

from rules_port.filters import records_filter_matches


# ECardAttributes.SpellShield (Mechanics/ECardAttributes.cs).
_SPELL_SHIELD_ATTR = 128


class _FilterContext(dict):
    """Dict state with client-shaped attributes for native filter leaves."""

    def __getattr__(self, name):
        return self.get(name)


def _targeting_immune(db, session_id, battle_state, card, source):
    """Port of ``AbilityTargetTemplate.IsTargetImmune``.

    A card may carry authored ``TargetingImmunityModifier`` rules whose filter
    matches the ability source.  Non-auto opposing permanents covered by such a
    rule are not legal targets.
    """
    try:
        from .static_rules import rule_modifiers
        rules = rule_modifiers(
            db, session_id, battle_state or {}, int(card.get("card_uid") or 0))
    except Exception:
        return False
    for rule in rules or ():
        if str(rule.get("property") or "") != "targetingimmunity":
            continue
        spec = rule.get("filter") or rule.get("cardfilter")
        if not spec:
            return True
        try:
            if records_filter_matches(
                    source or {}, spec, source=source or {},
                    context=dict(battle_state or {})):
                return True
        except Exception:
            continue
    return False


def _last(value):
    return str(value or "").rsplit(".", 1)[-1]


def _find_filter(node, kind):
    if isinstance(node, dict):
        if _last(node.get("_t")) == kind:
            return node
        for value in node.values():
            found = _find_filter(value, kind)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_filter(value, kind)
            if found is not None:
                return found
    return None


def _side(uid):
    return "ai" if not uid else "player"


def _shards(value):
    try:
        data = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    values = data if isinstance(data, list) else data.get("list", data.get("values", []))
    return [{0: 0, 1: 4, 2: 8, 3: 16, 4: 32, 5: 64}.get(int(item), 0)
            for item in (values or [])]


def shards_from_threshold(value):
    """Decode the typed threshold flags used by native effects."""
    return _shards(value)


def target_template(db, template_id):
    from pvp_db import db_target_template_row
    row = db_target_template_row(template_id, conn=db)
    if not row:
        return None
    return {"template_id": row[0], "is_auto_target": row[2],
            "is_random_target": row[3], "optional": row[4], "explicit": row[5],
            "player_filter": row[6] or "", "collection_flags": row[7] or "",
            "min_target_count": row[8], "max_target_count": row[9],
            "filter_json": row[10] or "{}", "target_kind": row[11] or ""}


def target_uses_both_players(db, template_id):
    template = target_template(db, template_id)
    return bool(template and str(template["player_filter"]).lower() not in
                {"self", "you", "controller"})


def implicit_champion_target(db, session, handler, battle_state, *,
                             opposing=False):
    """Resolve an authored implicit ``You``/opposing-champion target.

    Some damage modifiers have no explicit target slot.  The client derives
    their champion target from the first Records target template; keep that
    inference in RulesPort so native effects do not import the historical BOM
    target helper.
    """
    import json as _json
    from pvp_db import (db_ability_target_template_ids,
                        db_target_template_info,
                        db_target_template_targeting_info)

    ability_guid = str((battle_state or {}).get("resolving_ability") or "")
    if not ability_guid:
        return None
    payload = db_ability_target_template_ids(ability_guid, conn=db)
    try:
        template_ids = _json.loads(payload or "[]")
    except (TypeError, ValueError, _json.JSONDecodeError):
        return None
    if not template_ids:
        return None
    template_id = template_ids[0]
    info = db_target_template_info(template_id, conn=db)
    if not info:
        return None
    if not opposing:
        if str(info[1] or "") != "PlayerTargetTemplate":
            return None
    else:
        targeting = db_target_template_targeting_info(template_id, conn=db)
        try:
            filter_json = _json.loads((targeting[0] if targeting else "{}") or "{}")
        except (TypeError, ValueError, _json.JSONDecodeError):
            filter_json = {}

        def has_filter(node, wanted):
            if isinstance(node, dict):
                if _last(node.get("_t")) == wanted:
                    return True
                return any(has_filter(value, wanted) for value in node.values())
            if isinstance(node, list):
                return any(has_filter(value, wanted) for value in node)
            return False

        player_filter = str(targeting[1] if targeting else "").lower()
        if not (has_filter(filter_json, "IsHero") and (
                has_filter(filter_json, "IsNotControlledBy") or
                player_filter in {"opponent", "opposing"})):
            return None

    owner = (battle_state or {}).get("resolving_owner_id")
    if owner is None:
        owner = 0
    owner = int(owner)
    if (battle_state or {}).get("pvp"):
        champions = battle_state.get("champ_map") or {}
        if opposing:
            owners = [int(pid) for pid in champions if int(pid) != owner]
            owner = owners[0] if owners else owner
        value = champions.get(owner, champions.get(str(owner)))
        return int(value) if value is not None else None
    attr = "_ai_champ_scid" if (owner == 0) ^ opposing else "_player_champ_scid"
    champion = getattr(handler, attr, None)
    if champion is None:
        return None
    try:
        return int(champion.uid.uid64)
    except (AttributeError, TypeError, ValueError):
        return None


def _card(row, battle_state=None, db=None, session_id=None):
    row = tuple(row)
    if len(row) >= 19:
        (uid, card_type, location, owner, template_guid, state, attack, defense,
         name, cost, subtype, threshold, abilities, buffs, rarity, sockets, gems,
         original_guid, card_attributes) = row[:19]
    else:
        (uid, card_type, location, owner, template_guid, state, attack, defense,
         name, cost, subtype, threshold, abilities, buffs, rarity, sockets, gems,
         original_guid) = row[:18]
        card_attributes = 0
    try:
        saved = json.loads(buffs or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    attrs = saved.get("int_attrs") if isinstance(saved.get("int_attrs"), dict) else {}
    card = {"card_uid": int(uid), "card_type": card_type or "",
            "location": location or "", "user_id": int(owner or 0),
            "owner_id": int(owner or 0), "controller_id": int(owner or 0),
            "template_guid": template_guid or "", "state": int(state or 0),
            "attack": int(attack or 0), "defense": int(defense or 0),
            "name": name or "", "cost": int(cost or 0),
            "subtype": saved.get("subtype", subtype or ""),
            "attributes": int(saved.get("attributes", 0) or 0)
            | int(card_attributes or 0),
            "int_attrs": attrs, "shards": _shards(threshold),
            "rarity": rarity or "", "socket_count": int(sockets or 0),
            "gems": int(gems or 0), "card_abilities": json.loads(abilities or "[]")
            if isinstance(abilities, str) else (abilities or []),
            "counters": saved.get("counters", {}),
            "counter_guids": saved.get("counter_guids", {}),
            "parent_uid": int(saved.get("parent_uid", 0) or 0),
            "original_template_guid": original_guid or ""}
    if (db is not None and session_id is not None and
            not (battle_state or {}).get("_rules_port_suppress_card_properties")):
        from .static_rules import effective_card_properties
        thresholds, current_subtype = effective_card_properties(
            db, session_id, battle_state or {}, int(uid))
        card["shards"] = thresholds
        card["subtype"] = current_subtype
        card["thresholds"] = thresholds
    return card


def _source_card(db, session_id, source_uid, controller_uid):
    if source_uid is not None:
        from pvp_db import db_target_source_row
        row = db_target_source_row(session_id, int(source_uid), conn=db)
        if row:
            (uid, card_type, location, owner, state, attack, defense,
             template_guid, name, cost, subtype, threshold, attributes,
             rarity, sockets, gems, original_guid, abilities, buffs) = row
            return {"card_uid": int(uid), "card_type": card_type or "",
                    "location": location or "", "user_id": int(owner or 0),
                    "owner_id": int(owner or 0), "controller_id": int(owner or 0),
                    "template_guid": template_guid or "", "state": int(state or 0),
                    "attack": int(attack or 0), "defense": int(defense or 0),
                    "name": name or "", "cost": int(cost or 0),
                    "subtype": subtype or "", "attributes": int(attributes or 0),
                    "shards": _shards(threshold), "rarity": rarity or "",
                    "socket_count": int(sockets or 0), "gems": int(gems or 0),
                    "original_template_guid": original_guid or "",
                    "card_abilities": json.loads(abilities or "[]")
                    if isinstance(abilities, str) else (abilities or [])}
    return {"card_uid": int(source_uid or 0), "user_id": int(controller_uid or 0),
            "owner_id": int(controller_uid or 0), "controller_id": int(controller_uid or 0),
            "card_type": "Champion", "attack": 0, "defense": 0}


def evaluate_card_filter(card, spec, source_uid=None, *, ability_state=None,
                         db=None):
    """Evaluate one Records card filter against a projected card dict.

    Companion to :func:`legal_targets` for leaves that count filtered cards
    (``SetCardCountVariable``).  The native context imports this from
    ``rules_port.targeting``; it previously did not exist there and raised
    ImportError.
    """
    if not spec:
        return True
    source = None
    if source_uid is not None:
        try:
            source = {"card_uid": int(source_uid)}
        except (TypeError, ValueError):
            source = None
    try:
        return records_filter_matches(
            card, spec, source=source, context=ability_state or {})
    except (TypeError, ValueError, KeyError):
        return False


def legal_targets(db, session_id, controller_uid, template_id, source_uid,
                  both_players=False, champions=None, battle_state=None):
    template = target_template(db, template_id)
    if not template:
        return []
    if (_last(template.get("target_kind")) == "AbilitySourceCardTargetTemplate"
            and source_uid is not None):
        return [int(source_uid)]
    try:
        filter_json = json.loads(template["filter_json"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        filter_json = {}
    from pvp_db import db_target_candidate_rows
    top_n = _find_filter(filter_json, "TopNOfDeck")
    zones = [z.strip() for z in str(template["collection_flags"]).split("|") if z.strip()]
    zone_map = {"Warzone": "warzone", "Hand": "hand", "Deck": "deck",
                "Crypt": "discard", "Discard": "discard", "Void": "void",
                "Champions": "champions", "CastSpells": "CastSpells",
                "Underground": "underground"}
    zones = [zone_map.get(z, z.lower()) for z in zones] or [
        "warzone", "hand", "deck", "discard", "void", "underground"]
    if top_n is not None:
        zones = ["deck"]
    rows = db_target_candidate_rows(session_id, zones,
                                    controller_uid=controller_uid,
                                    both_players=both_players,
                                    top_n=top_n is not None, conn=db)
    player_filter = str(template["player_filter"]).lower()
    self_only = player_filter in {"self", "you", "controller"}
    opposing = player_filter in {"opponent", "opposing", "singleopponent", "multipleopponents"}
    is_auto = bool(template.get("is_auto_target"))
    source = _source_card(db, session_id, source_uid, controller_uid)
    source_owner = int((source or {}).get("user_id", controller_uid) or 0)
    cards = []
    by_owner = {}
    for row in rows:
        card = _card(row, battle_state, db, session_id)
        if self_only and card["user_id"] != int(controller_uid or 0):
            continue
        if opposing and card["user_id"] == int(controller_uid or 0):
            continue
        # C# AbilityTargetTemplate.IsCardValidTarget: a non-auto target on an
        # opposing permanent must not be Spell-Shielded, Spectral, or covered
        # by a TargetingImmunity rule.  Without these the picker/AI offered
        # untargetable cards as legal.
        permanent = str(card.get("location") or "").lower() in (
            "warzone", "champions")
        if (not is_auto and permanent and card["user_id"] != source_owner):
            if int(card.get("attributes", 0) or 0) & _SPELL_SHIELD_ATTR:
                continue
            if _targeting_immune(db, session_id, battle_state, card, source):
                continue
        if (int((card.get("int_attrs") or {}).get("Spectral", 0) or 0) >= 1
                and int(card["card_uid"]) != int(source_uid or 0)):
            continue
        cards.append(card)
        by_owner.setdefault(card["user_id"], []).append(card)
    def matches(card, spec, pool):
        context = _FilterContext(battle_state or {})
        context["cards"] = pool
        context["all_cards"] = pool
        context["active_player_id"] = (battle_state or {}).get(
            "active_player_id", controller_uid)
        # The activating player is the authoritative ``player`` operand for
        # cost/target filters.  Relying only on the source-card projection
        # makes an optional or partially materialized source look
        # uncontrolled, which suppresses valid payment candidates.
        return records_filter_matches(
            card, spec, source=source, context=context,
            player=int(controller_uid or 0))
    if top_n is not None:
        nested = top_n.get("m_Filter") or {}
        amount = int(top_n.get("m_Amount", 1) or 1)
        selected = []
        for owner_cards in by_owner.values():
            ordered = (list(reversed(owner_cards)) if
                       top_n.get("m_CountFromBottom") else list(owner_cards))
            if top_n.get("m_TopHalfOfDeck"):
                ordered = ordered[:(len(ordered) + 1) // 2]
            count = 0
            for card in ordered:
                if matches(card, nested, owner_cards):
                    selected.append(int(card["card_uid"]))
                    count += 1
                    if count >= amount:
                        break
        return selected


    out = [int(card["card_uid"]) for card in cards
           if matches(card, filter_json, cards)]
    if champions and ("champions" in zones or "IsHero" in str(filter_json)):
        for uid, owner, name, health in champions:
            if (not both_players and owner != controller_uid) or \
                    (self_only and owner != controller_uid) or \
                    (opposing and owner == controller_uid):
                continue
            card = {"card_uid": int(uid), "card_type": "Champion",
                    "location": "warzone", "user_id": owner,
                    "controller_id": owner, "name": name or "Champion",
                    "defense": int(health or 0), "attack": 0}
            if matches(card, filter_json, cards):
                out.append(int(uid))
    return out


def _target_ignore_acted_on(template_id):
    """Read C# SourceRevealedTargetTemplate.m_IgnoreActedOn from Records."""
    from gamedata import DEFAULT_RECORD_STORE
    rec = DEFAULT_RECORD_STORE.get("AbilityTargetTemplate", str(template_id).lower())
    if rec is None:
        return False
    try:
        data = rec.to_dict() if hasattr(rec, "to_dict") else rec
        return bool(data.get("m_IgnoreActedOn", False))
    except Exception:
        return False


def revealed_target_uids(db, session_id, owner_id, source_uid, template_id,
                         revealed_uids, battle_state=None, acted_on_uids=()):
    """Filter the authoritative reveal set through a Records target template.

    ``acted_on_uids`` are the cards the secondary target already acted on
    (C# ``SourceRevealedTargetTemplate.m_IgnoreActedOn``).  They are excluded
    from the result so "the remaining cards" does not re-move a card that the
    preceding play effect just put onto the chain.
    """
    template = target_template(db, template_id)
    if not template:
        return []
    try:
        spec = json.loads(template["filter_json"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        spec = {}
    ignores = {int(uid) for uid in (acted_on_uids or ())}
    from pvp_db import db_condition_card_row
    source = _source_card(db, session_id, int(source_uid or 0),
                          int(owner_id or 0))
    result = []
    for uid in revealed_uids or ():
        if int(uid) in ignores:
            continue
        row = db_condition_card_row(session_id, int(uid), conn=db)
        if not row:
            continue
        card = {
            "card_uid": int(row[0]), "card_type": row[1] or "",
            "location": row[2] or "", "user_id": int(row[3] or 0),
            "state": int(row[4] or 0), "attack": int(row[5] or 0),
            "defense": int(row[6] or 0), "template_guid": row[7] or "",
            "name": row[8] or "", "cost": int(row[9] or 0),
            "subtype": row[10] or "", "shards": _shards(row[11]),
            "attributes": int(row[12] or 0) | int(row[13] or 0),
            "src_owner_side": "player" if int(owner_id or 0) else "ai",
        }
        context = _FilterContext(battle_state or {})
        if records_filter_matches(card, spec, source=source,
                                  context=context):
            result.append(int(uid))
    return result


def legal_targets_for(db, session_id, controller_uid, target, source_uid, *,
                      both_players=None, champions=None, battle_state=None):
    template_id = getattr(target, "guid", target)
    if both_players is None:
        both_players = target_uses_both_players(db, template_id)
    return legal_targets(db, session_id, controller_uid, template_id, source_uid,
                         both_players=bool(both_players), champions=champions,
                         battle_state=battle_state)


def validate_target_selection(db, session_id, controller_uid, template_id,
                              source_uid, selected, both_players=False,
                              champions=None, battle_state=None):
    template = target_template(db, template_id)
    values = selected if isinstance(selected, (list, tuple)) else [selected]
    values = [int(v) for v in values if v is not None]
    if not template:
        return values
    if len(values) > int(template.get("max_target_count") or 1):
        return []
    if not values and template.get("optional"):
        return []
    legal = set(legal_targets(db, session_id, controller_uid, template_id, source_uid,
                              both_players=both_players, champions=champions,
                              battle_state=battle_state))
    if not values or any(value not in legal for value in values):
        return []
    if len(values) < int(template.get("min_target_count") or 0):
        return []
    return values


def ai_trigger_target(db, session, ability_guid, source_uid, owner_id,
                      battle_state, champions):
    """Choose the first legal typed target for an AI triggered ability."""
    from gamedata import DEFAULT_RECORD_STORE, ability_graph
    from pvp_db import db_ability_target_effect_rows, db_target_template_info
    graph = ability_graph(DEFAULT_RECORD_STORE, str(ability_guid).lower())
    target_ids = [target.guid for target in graph.targets] if graph else []
    for target_index, _effect_type in db_ability_target_effect_rows(
            ability_guid, conn=db):
        index = int(target_index)
        if index < 0 or index >= len(target_ids):
            continue
        template_id = target_ids[index]
        info = db_target_template_info(template_id, conn=db)
        if not info or int(info[2] or 0) or str(info[1] or "") in {
                "PlayerTargetTemplate", "AbilitySourceCardTargetTemplate",
                "SourceRevealedTargetTemplate", "SourceDrawnTargetTemplate",
                "SourceBuriedTargetTemplate", "SourceStoredTargetTemplate",
                "VoidedTargetTemplate", "AbilityCreatedTargetTemplate",
                "AbilityTriggerCardTargetTemplate"}:
            continue
        candidates = legal_targets(
            db, session.session_id, owner_id, template_id, source_uid,
            both_players=True, champions=champions, battle_state=battle_state)
        if candidates:
            return candidates[0]
    return None
