"""Data-driven targeting — a Python port of the client's
Game.Shared.Mechanics.Cards.Filters + Abilities.TargetTemplates evaluation,
driven by the target_templates table (seeded from AbilityTargetTemplate.jsonl).

This is the first ported layer of the client rules engine: instead of
hardcoded/heuristic target checks, legality comes from the gamedata target
template (explicit/auto, collection flags, min/max counts, card filter) — e.g.
Solitary Exile's Deploy "void another target card" is explicit, 1 target,
Warzone, any card type except the ability source.
"""

import json
import re
from pathlib import Path

import game_engine


ZONE_MAP = {
    "Warzone": "warzone",
    "Hand": "hand",
    "Deck": "deck",
    "Crypt": "discard",
    "Discard": "discard",
    "Void": "void",
    "Champions": "champions",
    "CastSpells": "CastSpells",
    "Underground": "underground",
    "PlayedResources": "PlayedResources",
    "Choosing": "choosing",
}

ALL_TARGET_ZONES = tuple(ZONE_MAP.values())

_TEMPLATE_FACTIONS = None


def template_faction(template_guid):
    """Return the authoritative faction for a card template.

    ``card_templates`` predates faction being materialized as a DB column.
    CardFilter.InFaction is nevertheless part of the typed gamedata and is
    used by Conscript and several random-card effects.  Read the extracted
    CardTemplate records lazily and cache the small GUID -> faction map.
    """
    global _TEMPLATE_FACTIONS
    guid = str(template_guid or "").lower()
    if not guid:
        return ""
    if _TEMPLATE_FACTIONS is None:
        _TEMPLATE_FACTIONS = {}
        path = Path(__file__).resolve().parents[2] / "Records" / \
            "CardTemplate.jsonl"
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        value = json.loads(line)
                        if isinstance(value, str):
                            value = json.loads(
                                re.sub(r",\s*([}\]])", r"\1", value))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    if not isinstance(value, dict):
                        continue
                    card_id = value.get("m_Id") or {}
                    card_guid = str(card_id.get("m_Guid") or "").lower()
                    if card_guid:
                        _TEMPLATE_FACTIONS[card_guid] = str(
                            value.get("m_Faction") or "")
        except OSError:
            pass
    return _TEMPLATE_FACTIONS.get(guid, "")


def shards_from_threshold(threshold_json):
    """Parse a template's threshold_json into ECardShards flags, e.g.
    {"list": [5]} -> [Diamond]."""
    try:
        d = json.loads(threshold_json or "{}")
    except Exception:
        return []
    idx_flags = {0: 0, 1: 4, 2: 8, 3: 16, 4: 32, 5: 64}
    if isinstance(d, list):
        items = d
    else:
        items = d.get("list") or d.get("values") or []
    return [idx_flags.get(int(i), 0) for i in items]


def _last(t):
    return str(t or "").split(".")[-1]


def _find_filter_type(node, filter_type):
    """Find the first metadata filter of *filter_type* in a filter tree."""
    if isinstance(node, dict):
        if _last(node.get("_t")) == filter_type:
            return node
        for child in node.values():
            found = _find_filter_type(child, filter_type)
            if found is not None:
                return found
    elif isinstance(node, list):
        for child in node:
            found = _find_filter_type(child, filter_type)
            if found is not None:
                return found
    return None


def evaluate_card_filter(card, filter_json, source_uid, stored_names=None,
                         source_card=None):
    """Evaluate a gamedata CardFilter tree against one card.

    ``card`` is a dict with at least card_uid, card_type, location, user_id,
    attack, defense.  Filters we cannot model (e.g. IsSubType without a
    subtype column) default to True so they never wrongly exclude.
    """
    if not isinstance(filter_json, dict):
        return True
    t = _last(filter_json.get("_t"))
    if t == "AndCardFilter":
        return all(evaluate_card_filter(card, f, source_uid, stored_names,
                                        source_card)
                   for f in filter_json.get("m_TargetFilters", []))
    if t == "OrCardFilter":
        return any(evaluate_card_filter(card, f, source_uid, stored_names,
                                        source_card)
                   for f in filter_json.get("m_TargetFilters", []))
    if t == "NotCardFilter":
        return not evaluate_card_filter(
            card, filter_json.get("m_TargetFilter", {}), source_uid,
            stored_names, source_card)
    if t == "IsType":
        wanted = set((filter_json.get("m_CardType", "") or "").split("|"))
        actual = set((card.get("card_type") or "").split("|"))
        # Some templates have composite card types, e.g. Argus is stored as
        # Troop|Artifact.  The client treats IsType as matching any component
        # of that type mask, not only an exact string match.
        return bool(wanted & actual)
    if t == "IsTroop":
        return "Troop" in (card.get("card_type") or "").split("|")
    if t == "IsArtifact":
        return "Artifact" in (card.get("card_type") or "").split("|")
    if t == "IsResource":
        return "Resource" in (card.get("card_type") or "").split("|")
    if t == "IsHero":
        return card.get("card_type") == "Champion"
    if t == "IsSubType":
        wanted = (filter_json.get("m_SubType") or "").lower()
        if not wanted:
            return True
        subs = {s.strip().lower() for s in
                (card.get("subtype") or "").split(" ") if s.strip()}
        return wanted in subs
    if t == "IsNotType":
        wanted = set((filter_json.get("m_CardType", "") or "").split("|"))
        actual = set((card.get("card_type") or "").split("|"))
        return not bool(wanted & actual)
    if t == "IsRarity":
        wanted = str(filter_json.get("m_Rarity") or "").lower()
        return not wanted or str(card.get("rarity") or "").lower() == wanted
    if t == "IsSocketable":
        op = filter_json.get("m_ComparisonOp", "Equals")
        value = int(filter_json.get("m_SocketValue", 0) or 0)
        sockets = int(card.get("socket_count", 0) or 0)
        return {"GreaterThanOrEqual": sockets >= value,
                "LessThanOrEqual": sockets <= value,
                "Equal": sockets == value,
                "Equals": sockets == value,
                "GreaterThan": sockets > value,
                "LessThan": sockets < value}.get(op, True)
    if t == "InFaction":
        wanted = (filter_json.get("m_Faction") or "").lower()
        actual = (card.get("faction") or "").lower()
        # If a fixture has no extracted faction data, preserve the existing
        # permissive behavior.  Live Records-backed games always populate it.
        return not wanted or not actual or actual == wanted
    if t == "IsAttacking":
        return bool(int(card.get("state", 0) or 0)
                    & int(game_engine.ECardStates.Attacking))
    if t == "IsBlocking":
        return bool(int(card.get("state", 0) or 0)
                    & int(game_engine.ECardStates.Blocking))
    if t == "IsDamagedThisTurn":
        return bool(int(card.get("state", 0) or 0)
                    & int(game_engine.ECardStates.Damaged))
    if t == "IsPlayedThisTurn":
        return bool(int(card.get("state", 0) or 0)
                    & int(game_engine.ECardStates.CameOutThisTurn))
    if t == "IsTapped":
        return bool(int(card.get("state", 0) or 0)
                    & int(game_engine.ECardStates.Tapped))
    if t in ("HasAllAttributeFlags", "HasAttribute"):
        wanted = (filter_json.get("m_CardAttributeFlags") or "")
        if not wanted:
            return True
        attrs = int(card.get("attributes", 0) or 0)
        for name in wanted.split("|"):
            if name == "Flight":
                flag = game_engine.ECardAttributes.Flight
            elif name == "SkyGuard":
                flag = game_engine.ECardAttributes.SkyGuard
            elif name == "SpellShield":
                flag = game_engine.ECardAttributes.SpellShield
            elif name == "Steadfast":
                flag = game_engine.ECardAttributes.Steadfast
            elif name == "SpiritDrain":
                flag = game_engine.ECardAttributes.SpiritDrain
            else:
                flag = getattr(game_engine.ECardAttributes, name, 0)
            if not flag or not (attrs & flag):
                return False
        return True
    if t == "HasAnyAttributeFlags":
        wanted = (filter_json.get("m_CardAttributeFlags") or "")
        if not wanted:
            return True
        attrs = int(card.get("attributes", 0) or 0)
        for name in wanted.split("|"):
            flag = getattr(game_engine.ECardAttributes, name, 0)
            if flag and attrs & flag:
                return True
        return False
    if t == "IntAttrFilter":
        # Int attributes are stored in the dynamic card state rather than in
        # the printed card template.  An absent attribute is the metadata
        # default of zero (not an unknown wildcard): this is what makes
        # ``Not(IntAttrFilter(Tamed >= 1))`` correctly select an untamed card
        # while ``IntAttrFilter(Untamed >= 1)`` still requires the marker.
        attr = str(filter_json.get("m_Attribute") or "")
        values = card.get("int_attrs") or {}
        actual = int(values.get(attr, 0) or 0)
        rhs = int(filter_json.get("m_Value", 0) or 0)
        if filter_json.get("m_CompareToCost"):
            rhs = int(card.get("cost", 0) or 0)
        op = filter_json.get("m_ComparisonOp", "Equals")
        return {"GreaterThanOrEqual": actual >= rhs,
                "LessThanOrEqual": actual <= rhs,
                "Equal": actual == rhs,
                "Equals": actual == rhs,
                "GreaterThan": actual > rhs,
                "LessThan": actual < rhs}.get(op, True)
    if t == "HasName":
        name = (filter_json.get("m_Name") or "").lower()
        if filter_json.get("m_UseStoredName"):
            names = stored_names or []
            if not names:
                return True
            name = (names[-1] or "").lower()
        if not name:
            return True
        return (card.get("name") or "").lower() == name
    if t == "IsAbilitySource":
        return card.get("card_uid") == int(source_uid or 0)
    if t == "IsCardName":
        name = (filter_json.get("m_CardName") or "").lower()
        # Dynamic names (for example #PET_SPIRIT_STAG#) remain placeholders
        # in the authoritative card-template data.  They are not wildcards:
        # the same placeholder identifies the token template that the client
        # later renders with the champion's pet name.  Treating them as a
        # match-all filter causes a pet-only modifier to target every card in
        # the collection, including opposing troops.
        if not name:
            return True
        return (card.get("name") or "").lower() == name
    if t == "IsControlledBy":
        # The serialized filter is the ownership part of phrases such as
        # "your deck" and "a troop you control".  ``player_filter`` on the
        # target template describes who may choose/receive a target; it does
        # not replace this card-filter ownership test.  The source controller
        # is supplied by legal_targets as ``src_owner_id``.
        expected_owner = card.get("src_owner_id")
        if filter_json.get("m_TestAgainstActivePlayer"):
            expected_owner = card.get("active_player_id", expected_owner)
        if expected_owner is None:
            return True
        return int(card.get("user_id", 0) or 0) == int(expected_owner or 0)
    if t == "NameContainsFilter":
        frag = (filter_json.get("m_ContainsString") or "").lower()
        if not frag:
            return True
        return frag in (card.get("name") or "").lower()
    if t == "IsNotControlledBy":
        # PvP player ids are both non-zero, so compare actual ids instead of
        # collapsing both players into the Practice player/AI side labels.
        src_owner_id = card.get("src_owner_id")
        if src_owner_id is not None:
            return int(card.get("user_id", 0)) != int(src_owner_id)
        src_side = card.get("src_owner_side")
        if not src_side:
            return True
        return _side_of(card.get("user_id")) != src_side
    if t == "HasSourceCastingCostFilter":
        op = filter_json.get("m_ComparisonOp", "GreaterThanOrEqual")
        # The client compares the candidate's cost with the ability source's
        # effective cost, then applies AddValue.  m_CastingCost is a legacy
        # serialized field and is not used by IsMatch.
        target = int(source_card.get("cost", 0) or 0) \
            if source_card else int(filter_json.get("m_CastingCost", 0) or 0)
        add_value = filter_json.get("m_AddValue")
        if isinstance(add_value, dict):
            # The common cost-relative filters use a constant or an ability
            # variable whose resolved value is supplied by the caller.
            add_value = add_value.get("m_Value", add_value.get(
                "m_DefaultValue", source_card.get("cost_delta", 0)
                if source_card else 0))
        try:
            target += int(add_value or 0)
        except (TypeError, ValueError):
            pass
        cost = int(card.get("cost", 0) or 0)
        return {"GreaterThanOrEqual": cost >= target,
                "LessThanOrEqual": cost <= target,
                "Equal": cost == target,
                "Equals": cost == target,
                "GreaterThan": cost > target,
                "LessThan": cost < target}.get(op, True)
    if t == "HasResourceCost":
        op = filter_json.get("m_ComparisonOp", "GreaterThanOrEqual")
        target = int(filter_json.get("m_ResourceCost", 0) or 0)
        cost = int(card.get("cost", 0) or 0)
        return {"GreaterThanOrEqual": cost >= target,
                "LessThanOrEqual": cost <= target,
                "Equal": cost == target,
                "Equals": cost == target,
                "GreaterThan": cost > target,
                "LessThan": cost < target}.get(op, True)
    if t == "IsColor":
        flags = (filter_json.get("m_ColorFlags") or "").lower()
        if not flags:
            return True
        wanted = game_engine.SHARD_TO_FLAG.get(flags, 0)
        if not wanted:
            return True
        return wanted in (card.get("shards") or [])
    if t == "DamagedOpponentThisTurn":
        return card.get("card_uid") in set(
            int(u) for u in (card.get("damaged_opponent_this_turn") or []))
    if t == "HasASharedShardWithSourceFilter":
        if not source_card:
            return True
        source_shards = set(source_card.get("shards") or [])
        card_shards = set(card.get("shards") or [])
        if not source_shards or not card_shards:
            return False
        if filter_json.get("m_ExactMatch"):
            return source_shards == card_shards
        # Colorless is not a shard and must not make an otherwise empty
        # intersection look like a match.
        return bool(source_shards & card_shards)
    if t == "HasSourceTypeFilter":
        if not source_card:
            return True
        if filter_json.get("m_DontExactlyMatchOriginal") and \
                card.get("template_guid") and \
                card.get("template_guid") == source_card.get("template_guid"):
            return False
        source_types = set((source_card.get("card_type") or "").split("|"))
        card_types = set((card.get("card_type") or "").split("|"))
        return bool(source_types & card_types)
    if t == "HasASharedRarityWithSourceFilter":
        if not source_card or not source_card.get("rarity"):
            return True
        return (card.get("rarity") or "").lower() == \
            str(source_card.get("rarity")).lower()
    if t == "HasASharedSubtypeWithSourceFilter":
        if not source_card:
            return True
        source_subtypes = {x.lower() for x in
                           str(source_card.get("subtype") or "").split()
                           if x}
        card_subtypes = {x.lower() for x in
                         str(card.get("subtype") or "").split() if x}
        return bool(source_subtypes & card_subtypes)
    if t == "CompareCastingCostToSourceCountersFilter":
        if not source_card:
            return True
        counter_name = ((filter_json.get("m_CounterType") or {}).get(
            "m_Guid") or "").lower()
        counters = source_card.get("counter_guids") or {}
        value = 0
        for name, count in (source_card.get("counters") or {}).items():
            if not counter_name or str(counters.get(name, "")).lower() == counter_name:
                value += int(count or 0)
        cost = int(card.get("cost", 0) or 0)
        op = filter_json.get("m_ComparisonOp", "Equal")
        return {"GreaterThanOrEqual": cost >= value,
                "LessThanOrEqual": cost <= value,
                "Equal": cost == value,
                "Equals": cost == value,
                "GreaterThan": cost > value,
                "LessThan": cost < value}.get(op, True)
    if t == "InZone":
        zones = (filter_json.get("m_Collection", "") or "").split("|")
        return (card.get("location") or "") in {ZONE_MAP.get(z, z.lower())
                                                for z in zones}
    if t == "IsControlledBy":
        # Use actual owner ids for PvP; retain the side fallback for Practice.
        src_owner_id = card.get("src_owner_id")
        if src_owner_id is not None:
            return int(card.get("user_id", 0)) == int(src_owner_id)
        src_side = card.get("src_owner_side")
        if not src_side:
            return True
        return _side_of(card.get("user_id")) == src_side
    if t == "HasAttackValue":
        op = filter_json.get("m_ComparisonOp", "")
        val = int(filter_json.get("m_AttackValue", 0) or 0)
        atk = int(card.get("attack", 0) or 0)
        return {"GreaterThanOrEqual": atk >= val,
                "LessThanOrEqual": atk <= val,
                "Equal": atk == val}.get(op, True)
    if t == "HasDefenseValue":
        op = filter_json.get("m_ComparisonOp", "")
        val = int(filter_json.get("m_DefenseValue", 0) or 0)
        def_ = int(card.get("defense", 0) or 0)
        return {"GreaterThanOrEqual": def_ >= val,
                "LessThanOrEqual": def_ <= val,
                "Equal": def_ == val}.get(op, True)
    # Unmodelled filter types (IsSubType, IsColor, TACFilter, ...) don't exclude.
    return True


def _side_of(user_id):
    return "ai" if not user_id else "player"




def target_template(db, template_id):
    """Return the target_templates row as a dict, or None."""
    row = db.execute(
        "SELECT template_id, game_text, is_auto_target, is_random_target, "
        "optional, explicit, player_filter, collection_flags, "
        "min_target_count, max_target_count, filter_json, target_kind "
        "FROM target_templates WHERE template_id=?", (template_id,)).fetchone()
    if not row:
        return None
    return {
        "template_id": row[0], "game_text": row[1],
        "is_auto_target": row[2], "is_random_target": row[3],
        "optional": row[4], "explicit": row[5],
        "player_filter": row[6], "collection_flags": row[7],
        "min_target_count": row[8], "max_target_count": row[9],
        "filter_json": row[10] or "{}",
        "target_kind": row[11] or "",
    }


def target_uses_both_players(db, template_id):
    """Return whether a target template may draw candidates from both sides.

    ``SinglePlayer`` describes cardinality, not ownership.  Ownership is
    expressed by the template's player filter and/or card filter.  The PvP
    option builders must therefore not turn every single-player target into a
    self-only target; doing so hides valid opposing champions and troops.
    """
    tpl = target_template(db, template_id)
    if not tpl:
        return False
    return (tpl.get("player_filter") or "").lower() not in {
        "self", "you", "controller",
    }


def legal_targets(db, session_id, controller_uid, template_id, source_uid,
                  both_players=False, champions=None, battle_state=None):
    """Return the card_uid list the client should be offered for a target
    template: every card in the template's collections that passes its filter.

    ``controller_uid`` is the DB user_id of the ability's controller (0 = AI).
    ``both_players`` includes the opponent's cards in the candidate pool (the
    target template's card filter decides legality — e.g. Solitary Exile's
    Deploy may void any warzone card that isn't itself).
    ``champions`` is an optional list of (card_uid, user_id, name, health)
    tuples for the two champions — the client can target champions (IsHero
    filters), so they join the candidate pool when the template allows it.
    ``battle_state`` optionally supplies transient per-turn filter data, such
    as the cards that dealt damage to an opposing champion this turn.
    """
    tpl = target_template(db, template_id)
    if not tpl:
        return []
    try:
        fjson = json.loads(tpl["filter_json"] or "{}")
    except Exception:
        fjson = {}
    top_n = _find_filter_type(fjson, "TopNOfDeck")

    # collection_flags is a visibility mask in the client data, not always
    # the actual target zone. TopNOfDeck is explicitly evaluated against a
    # controller's ordered deck and its nested filter decides which cards in
    # that deck qualify (Brightmoon Brave is the common example).
    if top_n is not None:
        zones = ["deck"]
    else:
        zones = [ZONE_MAP.get(z, z.lower())
                 for z in (tpl["collection_flags"] or "").split("|") if z]
    # Client semantics: None/empty collection flags means any collection,
    # rather than an empty legal-target set.  Keep the hidden/placeholder
    # collections out of this broad fallback.
    if not zones:
        zones = list(dict.fromkeys(ALL_TARGET_ZONES))
    player_filter = (tpl.get("player_filter") or "").lower()
    opposing = player_filter in {
        "opponent", "opposing", "singleopponent", "multipleopponents",
    }
    self_only = player_filter in {"self", "you", "controller"}
    wants_champions = any(z in ("champions", "warzone") for z in zones) or \
        "IsHero" in (tpl["filter_json"] or "")
    sql = ("SELECT gc.card_uid, gc.card_type, gc.location, gc.user_id, "
           "gc.card_state, "
           "COALESCE(ct.attack,0), COALESCE(ct.defense,0), "
           "ct.name, COALESCE(ct.cost,0), ct.subtype, ct.threshold_json, "
           "gc.card_abilities, gc.permanent_buffs "
           "FROM game_cards gc JOIN card_templates ct ON ct.guid = gc.template_guid "
           "WHERE gc.session_id=?")
    params = [session_id]
    placeholders = ",".join("?" * len(zones))
    sql += f" AND gc.location IN ({placeholders})"
    params += zones
    if not both_players:
        sql += " AND gc.user_id=?"
        params.append(controller_uid)
    sql += (" ORDER BY gc.user_id, gc.position" if top_n is not None
            else " ORDER BY gc.position")
    out = []
    top_n_by_owner = {}
    for cu, ctype, loc, uid, state, atk, def_, name, cost, subtype, thresh, card_abs, raw_buffs \
            in db.execute(sql, params):
        int_attrs = {}
        try:
            saved = json.loads(raw_buffs or "{}")
            persisted = saved.get("int_attrs", {}) if isinstance(saved, dict) else {}
            if isinstance(persisted, dict):
                int_attrs.update({str(k): int(v or 0) for k, v in persisted.items()})
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        try:
            for ability_guid in json.loads(card_abs or "[]"):
                for _eg, _et, _ep in db.execute(
                        "SELECT effect_guid,effect_type,param FROM ability_effects WHERE ability_guid=?",
                        (ability_guid,)).fetchall():
                    if _et != "CardModifierAbilityEffectTemplate":
                        continue
                    _pd = json.loads(_ep or "{}")
                    if str(_pd.get("property", "")).lower() == "intattr":
                        _attr = str(_pd.get("attribute") or "")
                        if not _attr:
                            low_text = str(_pd.get("text") or "").lower()
                            if "untamed" in low_text:
                                _attr = "Untamed"
                            elif "tamed" in low_text:
                                _attr = "Tamed"
                        if _attr:
                            int_attrs[_attr] = 1
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        if int_attrs.get("Tamed", 0) > 0:
            int_attrs.pop("Untamed", None)
        card = {"card_uid": int(cu), "card_type": ctype, "location": loc,
                "user_id": uid, "state": int(state or 0),
                "attack": atk, "defense": def_, "name": name or "",
                "cost": cost or 0, "src_owner_side": _side_of(controller_uid),
                "src_owner_id": controller_uid,
                "subtype": subtype or "",
                "int_attrs": int_attrs,
                "shards": shards_from_threshold(thresh)}
        if battle_state and battle_state.get("turn_player"):
            active = battle_state.get("turn_player")
            card["active_player_id"] = (
                controller_uid if active == _side_of(controller_uid) else
                (0 if _side_of(controller_uid) == "player" else
                 controller_uid))
        if battle_state:
            marker_turn = int(battle_state.get("damaged_opponent_turn", 0) or 0)
            current_turn = int(battle_state.get("turn_number", 0) or 0)
            if marker_turn == current_turn:
                card["damaged_opponent_this_turn"] = list(
                    battle_state.get("damaged_opponent_this_turn") or [])
        if self_only and int(uid or 0) != int(controller_uid or 0):
            continue
        if opposing and int(uid or 0) == int(controller_uid or 0):
            continue
        if top_n is not None:
            # TopNOfDeck wraps the actual card filter in m_Filter. Keep the
            # complete ordered deck here: Amount counts matching cards, while
            # TopHalfOfDeck limits the inspected portion of the deck before
            # applying the nested filter.
            top_n_by_owner.setdefault(int(uid or 0), []).append(card)
        elif evaluate_card_filter(card, fjson, source_uid):
            out.append(int(cu))

    if top_n is not None:
        amount = max(0, int(top_n.get("m_Amount", 1) or 1))
        nested = top_n.get("m_Filter") or {}
        selected = []
        for owner_cards in top_n_by_owner.values():
            if top_n.get("m_CountFromBottom"):
                owner_cards = list(reversed(owner_cards))
            if top_n.get("m_TopHalfOfDeck"):
                inspect = owner_cards[:(len(owner_cards) + 1) // 2]
                selected.extend(
                    int(card["card_uid"])
                    for card in inspect
                    if amount > 0 and
                    evaluate_card_filter(card, nested, source_uid))
            else:
                owner_count = 0
                for card in owner_cards:
                    if not evaluate_card_filter(card, nested, source_uid):
                        continue
                    selected.append(int(card["card_uid"]))
                    owner_count += 1
                    if owner_count >= amount:
                        break
        return selected
    if wants_champions and champions:
        for c_uid, c_owner, c_name, c_hp in champions:
            if not both_players and c_owner != controller_uid:
                continue
            champ_card = {"card_uid": int(c_uid), "card_type": "Champion",
                          "location": "warzone", "user_id": c_owner,
                          "state": 0, "attack": 0, "defense": c_hp,
                          "name": c_name or "Champion", "cost": 0,
                          "subtype": "", "shards": [],
                          "src_owner_side": _side_of(controller_uid),
                          "src_owner_id": controller_uid}
            if battle_state:
                marker_turn = int(
                    battle_state.get("damaged_opponent_turn", 0) or 0)
                current_turn = int(battle_state.get("turn_number", 0) or 0)
                if marker_turn == current_turn:
                    champ_card["damaged_opponent_this_turn"] = list(
                        battle_state.get("damaged_opponent_this_turn") or [])
            if self_only and int(c_owner or 0) != int(controller_uid or 0):
                continue
            if opposing and int(c_owner or 0) == int(controller_uid or 0):
                continue
            if evaluate_card_filter(champ_card, fjson, source_uid):
                out.append(int(c_uid))
    return out


def validate_target_selection(db, session_id, controller_uid, template_id,
                              source_uid, selected, both_players=False,
                              champions=None, battle_state=None):
    """Validate activation target IDs using the same metadata as auto-targets.

    The client rejects incomplete/illegal TargetInstances before applying an
    effect.  Server transaction data is untrusted, so explicit target maps
    must pass this check too.  The caller decides whether an empty result
    should open a prompt or make the effect a no-op.
    """
    tpl = target_template(db, template_id)
    values = selected if isinstance(selected, (list, tuple)) else [selected]
    values = [int(v) for v in values if v is not None]
    if tpl is None:
        return values
    if len(values) > int(tpl.get("max_target_count") or 1):
        return []
    if not values and tpl.get("optional"):
        return []
    legal = set(legal_targets(
        db, session_id, controller_uid, template_id, source_uid,
        both_players=both_players, champions=champions or [],
        battle_state=battle_state))
    if not values or any(v not in legal for v in values):
        return []
    minimum = int(tpl.get("min_target_count") or 0)
    if len(values) < minimum:
        return []
    return values
