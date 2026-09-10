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
_RECORD_GUIDS = {}
_EQUIPMENT_CARD_INFO = None
_EQUIPMENT_TYPES = None


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


def template_is_mercenary(template_guid):
    """Match IsMercenaryFilter against the extracted champion templates."""
    guid = str(template_guid or "").lower()
    if not guid:
        return False
    values = _RECORD_GUIDS.get("MercenaryTemplate")
    if values is None:
        values = set()
        path = Path(__file__).resolve().parents[2] / "Records" / \
            "MercenaryTemplate.jsonl"
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
                    ident = value.get("m_Id") or {}
                    if ident.get("m_Guid"):
                        values.add(str(ident["m_Guid"]).lower())
        except OSError:
            pass
        _RECORD_GUIDS["MercenaryTemplate"] = values
    return guid in values


def template_equipment_match(template_guid, equipment_type="None"):
    """Match the client's IsEquippedCardFilter from CardTemplate TAC data."""
    global _EQUIPMENT_CARD_INFO, _EQUIPMENT_TYPES
    guid = str(template_guid or "").lower()
    if not guid:
        return False
    if _EQUIPMENT_CARD_INFO is None:
        from .tac import _tac_attr_hash, decode_tac_tree
        _EQUIPMENT_CARD_INFO = {}
        path = Path(__file__).resolve().parents[2] / "Records" / \
            "CardTemplate.jsonl"
        required_hash = _tac_attr_hash("RequiredEquipment")
        guid_hash = _tac_attr_hash("Guid")
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
                    ident = value.get("m_Id") or {}
                    card_guid = str(ident.get("m_Guid") or "").lower()
                    if not card_guid:
                        continue
                    if not value.get("m_EquipmentModifiedCard"):
                        continue
                    tac = value.get("m_SerializedTAC") or {}
                    data = tac.get("data", "") if isinstance(tac, dict) else ""
                    tree = decode_tac_tree(data)
                    required = set()
                    for entry in tree.get(required_hash, []) if isinstance(
                            tree.get(required_hash), list) else []:
                        if isinstance(entry, dict) and isinstance(
                                entry.get(guid_hash), str):
                            required.add(entry[guid_hash].lower())
                    _EQUIPMENT_CARD_INFO[card_guid] = required
        except OSError:
            pass
    required = _EQUIPMENT_CARD_INFO.get(guid)
    if required is None:
        return False
    if str(equipment_type or "None").lower() == "none":
        return True
    if _EQUIPMENT_TYPES is None:
        _EQUIPMENT_TYPES = {}
        path = Path(__file__).resolve().parents[2] / "Records" / \
            "InventoryItemData.jsonl"
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if "InventoryEquipmentData" not in line:
                        continue
                    try:
                        value = json.loads(line)
                        if isinstance(value, str):
                            value = json.loads(
                                re.sub(r",\s*([}\]])", r"\1", value))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    if not isinstance(value, dict):
                        continue
                    ident = value.get("m_Id") or {}
                    item_guid = str(ident.get("m_Guid") or "").lower()
                    if item_guid and value.get("m_EquipmentType"):
                        _EQUIPMENT_TYPES[item_guid] = str(
                            value["m_EquipmentType"]).lower()
        except OSError:
            pass
    wanted = str(equipment_type).lower()
    return any(_EQUIPMENT_TYPES.get(item) == wanted for item in required)


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


def _blocking_targets(battle_state, source_uid):
    """Return troops currently blocking ``source_uid``.

    ``ai_blockers`` is the attacker -> blocker assignment used by PvE, while
    PvP stores the same relationship as ``blockers``. The key is the attacking
    troop and the values are the defending troops, regardless of which side is
    controlled by the AI. The client calls this relationship
    ``BlockingFilter(IsAbilitySource)``.
    """
    if not battle_state or source_uid is None:
        return set()
    try:
        source_uid = int(source_uid)
    except (TypeError, ValueError):
        return set()
    attacking = set()
    for key in ("ai_attackers", "player_attackers", "attackers"):
        for attacker in (battle_state.get(key) or {}):
            try:
                attacking.add(int(attacker))
            except (TypeError, ValueError):
                pass
    if source_uid not in attacking:
        return set()
    for blocker_map in (battle_state.get("ai_blockers") or {},
                        battle_state.get("blockers") or {}):
        for attacker, blockers in blocker_map.items():
            try:
                if int(attacker) != source_uid:
                    continue
            except (TypeError, ValueError):
                continue
            result = set()
            for blocker in blockers or []:
                try:
                    result.add(int(blocker))
                except (TypeError, ValueError):
                    continue
            return result
    return set()


def _compare_value(actual, op, expected):
    """Apply the client's EComparisons values to ordinary integers."""
    if op in ("OneLessThan", "OneMoreThan", "TwoMoreThan"):
        expected += {"OneLessThan": -1, "OneMoreThan": 1,
                     "TwoMoreThan": 2}[op]
        return actual == expected
    return {"GreaterThanOrEqual": actual >= expected,
            "LessThanOrEqual": actual <= expected,
            "Equal": actual == expected,
            "Equals": actual == expected,
            "GreaterThan": actual > expected,
            "LessThan": actual < expected}.get(op, True)


def _filter_zones(collections):
    return {ZONE_MAP.get(z, z.lower()) for z in
            str(collections or "").split("|") if z}


def _player_matches(card, source_card, player_filter):
    """Match Compare*Filter's EPlayerCardTargets against a live card."""
    owner = card.get("user_id")
    source_owner = (source_card or {}).get("src_owner_id",
                    (source_card or {}).get("user_id"))
    if str(player_filter or "").lower() in {"self", "you", "controller"}:
        return owner == source_owner
    if str(player_filter or "").lower() in {
            "opponent", "opposing", "singleopponent", "multipleopponents"}:
        return owner != source_owner
    return True


def _stored_uids(stored_names, ability_state):
    values = []
    for item in (ability_state or {}).get("stored_targets", {}).values():
        values.extend(item or [])
    for key in ("StoredTargets", "stored_targets"):
        values.extend((ability_state or {}).get(key) or [])
    if isinstance(stored_names, dict):
        values.extend(stored_names.get("uids") or [])
    try:
        return {int(value) for value in values}
    except (TypeError, ValueError):
        return set()


def evaluate_card_filter(card, filter_json, source_uid, stored_names=None,
                         source_card=None, card_pool=None, champion_pool=None,
                         ability_state=None, db=None):
    """Evaluate a gamedata CardFilter tree against one card.

    ``card`` is a dict with at least card_uid, card_type, location, user_id,
    attack, defense.  Filters we cannot model (e.g. IsSubType without a
    subtype column) default to True so they never wrongly exclude.
    """
    if not isinstance(filter_json, dict):
        return True
    t = _last(filter_json.get("_t"))
    if t == "AndCardFilter":
        return all(evaluate_card_filter(
            card, f, source_uid, stored_names, source_card, card_pool,
            champion_pool, ability_state, db)
                   for f in filter_json.get("m_TargetFilters", []))
    if t == "OrCardFilter":
        return any(evaluate_card_filter(
            card, f, source_uid, stored_names, source_card, card_pool,
            champion_pool, ability_state, db)
                   for f in filter_json.get("m_TargetFilters", []))
    if t == "NotCardFilter":
        return not evaluate_card_filter(
            card, filter_json.get("m_TargetFilter", {}), source_uid,
            stored_names, source_card, card_pool, champion_pool,
            ability_state, db)
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
    if t in ("BlockingFilter", "BeingBlockedByFilter"):
        state = ability_state or {}
        blocker_map = {}
        for key in ("ai_blockers", "blockers"):
            for attacker, blockers in (state.get(key) or {}).items():
                try:
                    blocker_map[int(attacker)] = {
                        int(blocker) for blocker in blockers or []}
                except (TypeError, ValueError):
                    continue
        card_uid = int(card.get("card_uid", 0) or 0)
        if t == "BlockingFilter":
            pairs = [(attacker, card_uid) for attacker, blockers in
                     blocker_map.items() if card_uid in blockers]
        else:
            pairs = [(card_uid, blocker) for blocker in next(
                (blockers for attacker, blockers in blocker_map.items()
                 if attacker == card_uid), set())]
        nested = filter_json.get("m_Filter") or {}
        if not nested:
            return bool(pairs)
        by_uid = {int(item.get("card_uid", 0)): item
                  for item in card_pool or []}
        if source_card:
            by_uid[int(source_card.get("card_uid", 0) or 0)] = source_card
        return any(attacker in by_uid and evaluate_card_filter(
            by_uid[attacker] if t == "BlockingFilter" else by_uid[blocker],
            nested, source_uid, stored_names, source_card, card_pool,
            champion_pool, ability_state, db)
                   for attacker, blocker in pairs)
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
        # effective cost, then applies AddValue. The current filter contract
        # has no independent serialized casting-cost input.
        target = int(source_card.get("cost", 0) or 0) if source_card else 0
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
    if t == "HasSourceResourceCost":
        # The client compares the candidate's effective resource cost with
        # the ability source's printed resource cost.  This is distinct from
        # HasSourceCastingCostFilter: the authored EComparisons value is the
        # relation itself (GreaterThan, OneLessThan, ...), not an additive
        # offset supplied by the caller.
        if not source_card:
            return True
        actual = int(card.get("cost", 0) or 0) + int(
            card.get("resource_x_cost_paid", 0) or 0)
        source_cost = int(source_card.get("cost", 0) or 0)
        op = str(filter_json.get("m_ComparisonOp") or "Equals")
        if op in ("OneLessThan", "OneMoreThan", "TwoMoreThan"):
            target = source_cost + {
                "OneLessThan": -1, "OneMoreThan": 1,
                "TwoMoreThan": 2,
            }[op]
            return actual == target
        return {"GreaterThanOrEqual": actual >= source_cost,
                "LessThanOrEqual": actual <= source_cost,
                "Equal": actual == source_cost,
                "Equals": actual == source_cost,
                "GreaterThan": actual > source_cost,
                "LessThan": actual < source_cost}.get(op, True)
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
    if t == "HasASharedClassWithSourceChampionFilter":
        if not source_card:
            return False
        champion_class = str(source_card.get("champion_class") or "").lower()
        if not champion_class:
            # Champion rows use subtype as their class in the fallback
            # representation used by campaign/PvP setup.
            champion_class = str(source_card.get("subtype") or "").lower()
        return bool(champion_class) and champion_class in \
            str(card.get("subtype") or "").lower()
    if t == "HasASharedSubtypeWithSourceChampionFilter":
        if not source_card:
            return False
        raw_subtypes = (source_card.get("champion_subtypes") or
                        source_card.get("champion_subtype") or
                        source_card.get("subtype") or "")
        if isinstance(raw_subtypes, str):
            raw_subtypes = raw_subtypes.split()
        source_subtypes = {str(value).lower() for value in raw_subtypes if value}
        card_subtypes = {str(value).lower() for value in
                         str(card.get("subtype") or "").split() if value}
        return bool(source_subtypes & card_subtypes)
    if t == "HasKeywordAbility":
        keyword = filter_json.get("m_Keyword") or ""
        for ability_guid in card.get("card_abilities") or []:
            if db is not None:
                from .triggers import ability_matches_keyword
                if ability_matches_keyword(db, ability_guid, keyword):
                    return True
            elif keyword.lower() in {str(value).lower() for value in
                                     card.get("keywords") or []}:
                return True
        return False
    if t == "IsSocketed":
        if filter_json.get("m_CompareToAbilitySource"):
            return bool(int(card.get("gems", 0) or 0) & int(
                (source_card or {}).get("gems", 0) or 0))
        count = int(card.get("gem_count", 0) or 0)
        if filter_json.get("m_MustBeMinor") and not card.get("gem_is_minor"):
            count = 0
        return _compare_value(count,
                              filter_json.get("m_ComparisonOp", "Equals"),
                              int(filter_json.get("m_SocketedValue", 0) or 0))
    if t == "IsMercenaryFilter":
        return card.get("card_type") == "Champion" and template_is_mercenary(
            card.get("template_guid"))
    if t == "IsEquippedCardFilter":
        return template_equipment_match(
            card.get("template_guid"), filter_json.get("m_EquipmentType"))
    if t == "InCollection":
        wanted = _filter_zones(filter_json.get("m_CardSource"))
        return not wanted or card.get("location") in wanted
    if t == "IsStoredCardFilter":
        return int(card.get("card_uid", 0) or 0) in _stored_uids(
            stored_names, ability_state)
    if t in ("IsChildOfAbilitySource", "IsChildOfAbilitySourceFilter"):
        return int(card.get("parent_uid", 0) or 0) == int(source_uid or 0)
    if t == "IsParentOfAbilitySourceFilter":
        return int((source_card or {}).get("parent_uid", 0) or 0) == int(
            card.get("card_uid", 0) or 0)
    if t == "OtherTroops":
        targeted = {int(value) for value in (ability_state or {}).get(
            "targeted_uids", []) if value is not None}
        return "Troop" in str(card.get("card_type") or "").split("|") and \
            int(card.get("card_uid", 0) or 0) not in targeted
    if t == "PlayersWhoControlMatchingFilter":
        if card.get("card_type") != "Champion":
            return False
        pool = card_pool or []
        collection = _filter_zones(filter_json.get("m_CardCollection"))
        target_filter = filter_json.get("m_TargetFilter") or {}
        count = sum(1 for candidate in pool
                    if candidate.get("user_id") == card.get("user_id")
                    and candidate.get("location") in collection
                    and evaluate_card_filter(
                        candidate, target_filter, source_uid, stored_names,
                        source_card, pool, champion_pool, ability_state, db))
        return _compare_value(count,
                              filter_json.get("m_ComparisonOp", "Equals"),
                              int(filter_json.get("m_RequiredQuantity", 0) or 0))
    if t == "TACFilter":
        serialized = filter_json.get("m_SerializedTAC") or {}
        data = serialized.get("data", "") if isinstance(serialized, dict) \
            else serialized
        from .tac import tac_string
        name = tac_string(data, "Name").lower()
        if name == "isquick":
            return bool({"QuickAction", "Quick"} & set(
                str(card.get("card_type") or "").split("|")))
        if name == "isbasic":
            return not bool({"QuickAction", "Quick"} & set(
                str(card.get("card_type") or "").split("|")))
        if name == "playermeetsthresholdrequirementstocast":
            thresholds = (ability_state or {}).get(
                "player_threshold" if _side_of(card.get("user_id")) == "player"
                else "ai_threshold", {})
            return all(int(thresholds.get(shard, thresholds.get(str(shard), 0)) or 0)
                       > 0 for shard in card.get("shards") or [])
        return False
    if t in ("CompareAttackToHighestFilter", "CompareAttackToLowestFilter",
             "CompareResourceCostToHighestFilter"):
        pool = card_pool or [card]
        wanted_zones = _filter_zones(filter_json.get("m_CollectionFlags"))
        nested = filter_json.get("m_CardFilter") or {}
        values = []
        for candidate in pool:
            if wanted_zones and candidate.get("location") not in wanted_zones:
                continue
            if not _player_matches(candidate, source_card,
                                   filter_json.get("m_PlayerFilter")):
                continue
            if not evaluate_card_filter(candidate, nested, source_uid,
                                        stored_names, source_card, pool,
                                        champion_pool, ability_state, db):
                continue
            field = "cost" if t == "CompareResourceCostToHighestFilter" \
                else "attack"
            values.append(int(candidate.get(field, 0) or 0))
        if not values:
            return False
        extreme = max(values) if "Highest" in t else min(values)
        field = "cost" if t == "CompareResourceCostToHighestFilter" else "attack"
        return _compare_value(int(card.get(field, 0) or 0),
                              filter_json.get("m_ComparisonOp", "Equals"),
                              extreme)
    if t == "CompareResourceCostToMyHighestFilter":
        pool = card_pool or [card]
        source_owner = (source_card or {}).get("src_owner_id",
                        (source_card or {}).get("user_id"))
        values = [int(candidate.get("cost", 0) or 0) for candidate in pool
                  if candidate.get("user_id") == source_owner and
                  candidate.get("location") == "warzone"]
        if not values:
            return False
        return _compare_value(int(card.get("cost", 0) or 0),
                              filter_json.get("m_ComparisonOp", "Equals"),
                              max(values))
    if t == "CompareHealthToHighestFilter":
        pool = champion_pool or []
        values = [int(champ[3] if not isinstance(champ, dict)
                       else champ.get("defense", 0) or 0) for champ in pool]
        if not values:
            return False
        return _compare_value(int(card.get("defense", 0) or 0),
                              filter_json.get("m_ComparisonOp", "Equals"),
                              max(values))
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
    if t == "HasCountersValue":
        # Counter values are keyed by the extracted counter-template GUID for
        # champions and by name+GUID metadata for ordinary game cards.
        wanted = str((filter_json.get("m_CounterType") or {}).get(
            "m_Guid") or "").lower()
        counters = card.get("counters") or {}
        guids = card.get("counter_guids") or {}
        value = 0
        for name, count in counters.items():
            if not wanted or str(name).lower() == wanted or \
                    str(guids.get(name, "")).lower() == wanted:
                try:
                    value += int(count or 0)
                except (TypeError, ValueError):
                    continue
        op = filter_json.get("m_ComparisonOp", "GreaterThanOrEqual")
        target = int(filter_json.get("m_Amount", 0) or 0)
        return {"GreaterThanOrEqual": value >= target,
                "LessThanOrEqual": value <= target,
                "Equal": value == target,
                "Equals": value == target,
                "GreaterThan": value > target,
                "LessThan": value < target}.get(op, True)
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

    # A few isolated targeting fixtures intentionally use the pre-socket
    # schema.  Build optional projections rather than making the evaluator
    # depend on the newest runtime DB shape.
    gc_columns = {row[1] for row in db.execute(
        "PRAGMA table_info(game_cards)").fetchall()}
    ct_columns = {row[1] for row in db.execute(
        "PRAGMA table_info(card_templates)").fetchall()}
    gc_gems = "gc.gems" if "gems" in gc_columns else "0"
    gc_original = ("gc.original_template_guid"
                   if "original_template_guid" in gc_columns else "''")
    ct_rarity = "ct.rarity" if "rarity" in ct_columns else "''"
    ct_sockets = "ct.socket_count" if "socket_count" in ct_columns else "0"

    # Filters such as HasSourceResourceCost and shared-source filters are
    # evaluated against the live source card by the original client.  Build
    # that context once and pass it through every candidate evaluation below;
    # previously those filters silently saw ``None`` and defaulted open.
    source_card = None
    if source_uid is not None:
        source_row = db.execute(
            "SELECT gc.card_uid, gc.card_type, gc.location, gc.user_id, "
            "gc.card_state, COALESCE(ct.attack,0), COALESCE(ct.defense,0), "
            "gc.template_guid, ct.name, COALESCE(ct.cost,0), ct.subtype, "
            "ct.threshold_json, gc.card_attributes, %s, %s, %s, %s, %s, "
            "gc.permanent_buffs "
            "FROM game_cards gc LEFT JOIN card_templates ct "
            "ON ct.guid=gc.template_guid "
            "WHERE gc.session_id=? AND gc.card_uid=?" %
            (ct_rarity, ct_sockets, gc_gems, gc_original,
             "gc.card_abilities" if "card_abilities" in gc_columns else "'[]'"),
            (session_id, int(source_uid))).fetchone()
        if source_row:
            source_card = {
                "card_uid": int(source_row[0]),
                "card_type": source_row[1] or "",
                "location": source_row[2] or "",
                "user_id": source_row[3],
                "state": int(source_row[4] or 0),
                "attack": int(source_row[5] or 0),
                "defense": int(source_row[6] or 0),
                "template_guid": source_row[7] or "",
                "name": source_row[8] or "",
                "cost": int(source_row[9] or 0),
                "subtype": source_row[10] or "",
                "rarity": "",
                "shards": shards_from_threshold(source_row[11]),
                "attributes": int(source_row[12] or 0),
                "faction": template_faction(source_row[7]),
                "rarity": source_row[13] or "",
                "socket_count": int(source_row[14] or 0),
                "gems": int(source_row[15] or 0),
                "original_template_guid": source_row[16] or "",
                "card_abilities": json.loads(source_row[17] or "[]")
                if isinstance(source_row[17], str) else (source_row[17] or []),
                "counters": {}, "counter_guids": {}, "int_attrs": {},
            }
            try:
                source_buffs = json.loads(source_row[18] or "{}")
                source_card["parent_uid"] = int(
                    source_buffs.get("parent_uid", 0) or 0)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
    if source_card is None:
        # Champion abilities use a SessionCardId that is not necessarily
        # materialized in game_cards.  Keep ownership available to typed
        # filters even when the source's printed card row is absent.
        source_card = {"card_uid": int(source_uid or 0),
                       "user_id": controller_uid,
                       "src_owner_id": controller_uid,
                       "src_owner_side": _side_of(controller_uid),
                       "card_type": "Champion", "subtype": "",
                       "shards": [], "card_abilities": []}
    top_n = _find_filter_type(fjson, "TopNOfDeck")
    blocking_filter = _find_filter_type(fjson, "BlockingFilter")
    blocking_targets = _blocking_targets(battle_state, source_uid) \
        if blocking_filter is not None else None

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
           "gc.template_guid, gc.card_state, "
           "COALESCE(ct.attack,0), COALESCE(ct.defense,0), "
           "ct.name, COALESCE(ct.cost,0), ct.subtype, ct.threshold_json, "
           "gc.card_abilities, gc.permanent_buffs, %s, %s, %s, %s "
           "FROM game_cards gc JOIN card_templates ct ON ct.guid = gc.template_guid "
           "WHERE gc.session_id=?" %
           (ct_rarity, ct_sockets, gc_gems, gc_original))
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
    candidate_cards = []
    top_n_by_owner = {}
    for cu, ctype, loc, uid, template_guid, state, atk, def_, name, cost, subtype, thresh, card_abs, raw_buffs, rarity, socket_count, gems, original_template_guid \
            in db.execute(sql, params):
        int_attrs = {}
        counters = {}
        counter_guids = {}
        try:
            saved = json.loads(raw_buffs or "{}")
            persisted = saved.get("int_attrs", {}) if isinstance(saved, dict) else {}
            if isinstance(persisted, dict):
                int_attrs.update({str(k): int(v or 0) for k, v in persisted.items()})
            if isinstance(saved, dict):
                if isinstance(saved.get("counters"), dict):
                    counters = dict(saved["counters"])
                if isinstance(saved.get("counter_guids"), dict):
                    counter_guids = dict(saved["counter_guids"])
            parent_uid = int(saved.get("parent_uid", 0) or 0) \
                if isinstance(saved, dict) else 0
        except (TypeError, ValueError, json.JSONDecodeError):
            parent_uid = 0
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
        if blocking_targets is not None and int(cu) not in blocking_targets:
            continue
        card = {"card_uid": int(cu), "card_type": ctype, "location": loc,
                "user_id": uid, "state": int(state or 0),
                "attack": atk, "defense": def_, "name": name or "",
                "cost": cost or 0, "src_owner_side": _side_of(controller_uid),
                "src_owner_id": controller_uid,
                "subtype": subtype or "",
                "int_attrs": int_attrs,
                "faction": template_faction(template_guid),
                "rarity": rarity or "",
                "socket_count": int(socket_count or 0),
                "gems": int(gems or 0),
                "gem_count": 1 if int(gems or 0) else 0,
                "original_template_guid": original_template_guid or "",
                "parent_uid": parent_uid,
                "card_abilities": json.loads(card_abs or "[]")
                if isinstance(card_abs, str) else (card_abs or []),
                "counters": counters,
                "counter_guids": counter_guids,
                "shards": shards_from_threshold(thresh)}
        if int(gems or 0):
            try:
                gem_row = db.execute(
                    "SELECT gem_type_name FROM gem_templates WHERE gem_type=?",
                    (int(gems),)).fetchone()
                card["gem_is_minor"] = bool(
                    gem_row and "minor" in str(gem_row[0] or "").lower())
            except Exception:
                card["gem_is_minor"] = False
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
        else:
            candidate_cards.append(card)

    # Comparison filters inspect a collection that may be broader than the
    # target template's own collection flags.  The current candidate pool is
    # still the correct fallback for small fixtures and ordinary targets; the
    # resolver supplies the complete live collection when it is available.
    for card in candidate_cards:
        if evaluate_card_filter(card, fjson, source_uid,
                                source_card=source_card,
                                card_pool=candidate_cards,
                                champion_pool=champions or [],
                                ability_state=battle_state, db=db):
            out.append(int(card["card_uid"]))

    if top_n is not None:
        nested = top_n.get("m_Filter") or {}
        selected = []
        for owner_cards in top_n_by_owner.values():
            if top_n.get("m_CountFromBottom"):
                owner_cards = list(reversed(owner_cards))
            amount = int(top_n.get("m_Amount", 1) or 1)
            owner = owner_cards[0].get("user_id") if owner_cards else controller_uid
            if top_n.get("m_AddSapphire"):
                thresholds = (battle_state or {}).get(
                    "player_threshold" if _side_of(owner) == "player"
                    else "ai_threshold", {})
                amount += int(thresholds.get(16, thresholds.get("16", 0)) or 0)
            if top_n.get("m_AddX"):
                amount += int((battle_state or {}).get(
                    "x_cost_paid", (source_card or {}).get(
                        "card_x_cost_paid", 0)) or 0)
            if top_n.get("m_AddRemovedCounters"):
                amount += int((battle_state or {}).get(
                    "counter_cost_paid", 0) or 0) * int(
                        top_n.get("m_AddRemovedCountersMultiplier", 1) or 1)
            if top_n.get("m_AddSourceCardsAttack"):
                amount += int((source_card or {}).get("attack", 0) or 0)
            if top_n.get("m_AddSourceCardsDefense"):
                amount += int((source_card or {}).get("defense", 0) or 0)
            if top_n.get("m_AddSourceCardsCost"):
                amount += int((source_card or {}).get("cost", 0) or 0)
            if top_n.get("m_AddDamageDealt"):
                amount += int((battle_state or {}).get("damage_dealt", 0) or 0)
            if top_n.get("m_AddDamageThatWouldBeDealt"):
                amount += int((battle_state or {}).get(
                    "damage_that_would_be_dealt", 0) or 0)
            variable = top_n.get("m_AddCardIntegerVariable") or ""
            if variable:
                amount += int((source_card or {}).get("int_attrs", {}).get(
                    variable, 0) or 0)
            amount = max(0, amount)
            if top_n.get("m_TopHalfOfDeck"):
                inspect = owner_cards[:(len(owner_cards) + 1) // 2]
                selected.extend(
                    int(card["card_uid"])
                    for card in inspect
                    if amount > 0 and
                    evaluate_card_filter(card, nested, source_uid,
                                         source_card=source_card,
                                         card_pool=owner_cards,
                                         ability_state=battle_state, db=db))
            else:
                owner_count = 0
                for card in owner_cards:
                    if not evaluate_card_filter(card, nested, source_uid,
                                                source_card=source_card,
                                                card_pool=owner_cards,
                                                ability_state=battle_state,
                                                db=db):
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
            if blocking_targets is not None and int(c_uid) not in blocking_targets:
                continue
            champ_card = {"card_uid": int(c_uid), "card_type": "Champion",
                          "location": "warzone", "user_id": c_owner,
                          "state": 0, "attack": 0, "defense": c_hp,
                          "name": c_name or "Champion", "cost": 0,
                          "subtype": "", "shards": [],
                          "faction": "",
                          "src_owner_side": _side_of(controller_uid),
                          "src_owner_id": controller_uid,
                          "gem_count": 0, "card_abilities": []}
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
            if evaluate_card_filter(champ_card, fjson, source_uid,
                                    source_card=source_card,
                                    card_pool=candidate_cards,
                                    champion_pool=champions or [],
                                    ability_state=battle_state, db=db):
                out.append(int(c_uid))
    return out


def legal_targets_for(db, session_id, controller_uid, target, source_uid,
                      *, both_players=None, champions=None,
                      battle_state=None):
    """Evaluate one ``AbilityBuilder`` target through the shared predicate.

    Callers in PvE and PvP used to repeat the same conversion from a typed
    target object to ``legal_targets`` arguments.  This helper centralizes
    that boundary while keeping the actual filter evaluator above as the
    single source of target legality.
    """
    template_id = getattr(target, "guid", target)
    if both_players is None:
        both_players = target_uses_both_players(db, template_id)
    return legal_targets(
        db, session_id, controller_uid, template_id, source_uid,
        both_players=bool(both_players), champions=champions,
        battle_state=battle_state)


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
