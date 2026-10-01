"""RulesPort-owned target candidate and selection evaluation.

Target legality is a rules decision, so live sessions must not call the
historical ``abilities.framework.targeting`` implementation.  This module
keeps the database projection at the edge and evaluates the extracted
Records filter through the native filter port.
"""

from __future__ import annotations

import json
from typing import Any, cast

from rules_port.filters import (records_filter_evaluator,
                                records_filter_matches)


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


def _protected_target(db, session_id, battle_state, card, source, source_owner,
                      is_auto):
    """C# ``AbilityTargetTemplate.IsCardValidTarget`` protections.

    A non-auto target on an opposing permanent (troop, artifact or champion)
    must not be Spell-Shielded, Stealth-Spellshielded, or covered by an
    authored TargetingImmunity rule.  Champions are not ``game_cards`` rows,
    so the Stealth keyword reads them from the persisted battle state.
    """
    if is_auto:
        return False
    if str(card.get("location") or "").lower() not in ("warzone", "champions"):
        return False
    if int(card.get("user_id", 0) or 0) == int(source_owner or 0):
        return False
    if int(card.get("attributes", 0) or 0) & _SPELL_SHIELD_ATTR:
        return True
    from .stealth import champion_target_is_spellshielded
    if champion_target_is_spellshielded(battle_state, card):
        return True
    return _targeting_immune(db, session_id, battle_state, card, source)


def _is_spectral(db, session_id, battle_state, card, source, controller_uid):
    """Evaluate the client's always-on spectral target restriction."""
    if int(card.get("card_uid") or 0) == int((source or {}).get("card_uid") or 0):
        return False
    return int((card.get("int_attrs") or {}).get("Spectral", 0) or 0) > 0


def _target_owner(card):
    try:
        return int(card.get("controller_id", card.get("user_id", 0)) or 0)
    except (AttributeError, TypeError, ValueError):
        return 0


def _player_filter_accepts(player_filter, target_owner, responsible_owner):
    """Mirror ``AbilityTargetTemplate.IsCardValidTarget`` player checks."""
    kind = str(player_filter or "").rsplit(".", 1)[-1].lower()
    if kind in {"self", "you", "controller"}:
        return int(target_owner) == int(responsible_owner)
    if kind in {"singleopponent", "multipleopponents", "opponent",
                "opposing"}:
        return int(target_owner) != int(responsible_owner)
    if kind in {"singleplayer", "multipleplayers", "allplayers"}:
        return True
    return False


def _target_in_collection(card, template):
    """Mirror the direct target validation collection check.

    A None mask is permissive in ``IsCardValidTarget`` even though the base
    enumerator returns no candidate for it. Some derived templates override
    one or both sides of that contract.
    """
    zones = template_zones(template)
    if not zones:
        return True
    return str(card.get("location") or "").lower() in {
        str(zone).lower() for zone in zones}


def _special_target_card(db, session_id, uid, controller_uid, state,
                         champions=None):
    """Resolve a selected SessionCardId, including synthetic champions."""
    card = _source_card(db, session_id, int(uid), controller_uid)
    champ_owners = {}
    for owner, champion_uid in (state.get("champ_map") or {}).items():
        try:
            champ_owners[int(champion_uid)] = int(owner)
        except (TypeError, ValueError):
            continue
    for row in champions or ():
        try:
            champ_owners[int(row[0])] = int(row[1])
        except (IndexError, TypeError, ValueError):
            continue
    owner = champ_owners.get(int(uid))
    if owner is not None:
        matching = next((row for row in (champions or ())
                         if int(row[0]) == int(uid)), None)
        name = matching[2] if matching and len(matching) > 2 else "Champion"
        health = matching[3] if matching and len(matching) > 3 else 0
        return {"card_uid": int(uid), "card_type": "Champion",
                "location": "champions", "user_id": owner,
                "owner_id": owner, "controller_id": owner,
                "name": name or "Champion", "defense": int(health or 0),
                "attack": 0, "attributes": 0, "int_attrs": {}}
    return card


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


def _requires_global_filter_pool(node):
    """Whether a filter operand scans another zone or trigger-card identity."""
    if isinstance(node, dict):
        kind = _last(node.get("_t"))
        if kind == "HasResourceCost" and (
                node.get("m_ResourceCostCardFilter") is not None or
                bool(node.get("m_AddSumListAttrName"))):
            return True
        if kind == "HasAttackValue" and (
                node.get("m_CompareToSourceControlledCardFilterCount") is not None or
                bool(node.get("m_CompareToStoredTarget")) or
                bool(node.get("m_CompareToTriggerSource"))):
            return True
        if kind == "IsCardName" and (
                bool(node.get("m_CompareToTriggerSource")) or
                bool(node.get("m_CompareToTriggerTarget"))):
            return True
        return any(_requires_global_filter_pool(value)
                   for value in node.values())
    if isinstance(node, list):
        return any(_requires_global_filter_pool(value) for value in node)
    return False


def _side(uid):
    return "ai" if not uid else "player"


def filter_restricts_to_zone(node, zone):
    """Return whether a Records card filter restricts cards to ``zone``.

    ``collection_flags`` is a visibility mask and is commonly the union of
    every collection a card could sit in, so only a nested ``InZone`` filter
    is the authoritative zone restriction.  Scheme ("choose an action in your
    deck") advertises ``Choosing`` in its visibility mask while its filter is
    ``InZone: Deck``; Corinth's choice-zone picker is the opposite.
    """
    if isinstance(node, dict):
        if (_last(node.get("_t")) == "InZone" and
                str(node.get("m_Collection") or "").lower() ==
                str(zone).lower()):
            return True
        return any(filter_restricts_to_zone(value, zone)
                   for value in node.values())
    if isinstance(node, list):
        return any(filter_restricts_to_zone(value, zone) for value in node)
    return False


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


def _target_record(template_id):
    """Return the immutable authored target record for one target template.

    ``target_templates`` is a client projection and intentionally omits fields
    added by specialized target classes.  Resolve those fields from Records at
    the interpreter boundary instead of extending SQLite or guessing from the
    display text.
    """
    from gamedata import DEFAULT_RECORD_STORE
    return DEFAULT_RECORD_STORE.get(
        "AbilityTargetTemplate", str(template_id or "").lower())


def _target_field(template_id, name, default=None):
    record = _target_record(template_id)
    if record is None:
        return default
    try:
        return record.field(name, default)
    except AttributeError:
        return default


def _target_spec(template_id):
    record = _target_record(template_id)
    return getattr(record, "target_spec", None) if record is not None else None


def target_template(db, template_id):
    from pvp_db import db_target_template_row
    row = db_target_template_row(template_id, conn=db)
    if not row:
        return None
    spec = _target_spec(template_id)
    return {"template_id": row[0], "is_auto_target": row[2],
            "is_random_target": row[3], "optional": row[4], "explicit": row[5],
            "player_filter": row[6] or "", "collection_flags": row[7] or "",
            "min_target_count": row[8], "max_target_count": row[9],
            "filter_json": row[10] or "{}", "target_kind": row[11] or "",
            "allow_best_effort_minimum": bool(
                getattr(spec, "allow_best_effort_minimum", False)),
            "min_variable": str(getattr(spec, "min_variable", "") or ""),
            "max_variable": str(getattr(spec, "max_variable", "") or "")}


def target_uses_both_players(db, template_id):
    template = target_template(db, template_id)
    return bool(template and str(template["player_filter"]).lower() not in
                {"self", "you", "controller"})


# ECardCollections names -> runtime zone names for authored target templates.
_ZONE_MAP = {"Warzone": "warzone", "Hand": "hand", "Deck": "deck",
             "Crypt": "discard", "Discard": "discard", "Void": "void",
             "Champions": "champions", "CastSpells": "CastSpells",
             "PlayedResources": "PlayedResources",
             "Underground": "underground", "Choosing": "choosing",
             "Mod": "mod", "Simulacrum": "simulacrum"}


def template_zones(template):
    """Return one authored template's collection flags as runtime zone names."""
    raw = str((template or {}).get("collection_flags") or "")
    if not raw.strip() or raw.strip().lower() in {"none", "null"}:
        # C# enumerates every collection when ECardCollections.None is
        # authored; it does not mean a collection named "None".
        return []
    zones = [zone.strip() for zone in raw.split("|") if zone.strip()]
    return [_ZONE_MAP.get(zone, zone.lower()) for zone in zones]


def template_targets_champions(template):
    """Whether an authored target template can select a champion in play.

    Champions are synthetic SessionCardIds with no ``game_cards`` row, so a
    candidate pool only contains them when the template names the Champions
    collection or filters with the client's ``IsHero`` filter.  Continuous
    champion-scoped rules read the same contract when deciding whether a
    static leaf applies to a champion rather than to a card in a zone.
    """
    if not template:
        return False
    try:
        filter_json = json.loads(template.get("filter_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        filter_json = {}
    return (_last(template.get("target_kind")) == "PlayerTargetTemplate" or
            "champions" in template_zones(template) or
            _find_filter(filter_json, "IsHero") is not None)


def implicit_champion_target(db, session, handler, battle_state, *,
                             opposing=False, template_id=None):
    """Resolve an authored implicit ``You``/opposing-champion target.

    Some damage modifiers have no explicit target slot.  The client derives
    their champion target from the first Records target template; keep that
    inference in RulesPort so native effects do not import the historical BOM
    target helper.  A resolver that already knows which authored target slot
    it is resolving (for example Booby Trap's target-index-1 ``You``) passes
    that ``template_id`` explicitly, because deriving it from the first
    template picked the ability's ``Self`` target instead.
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
    if template_id is None:
        template_id = template_ids[0]
    elif str(template_id).lower() not in {str(value).lower()
                                          for value in template_ids}:
        return None
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
    row_data: Any = tuple(row)
    if len(row_data) >= 19:
        (uid, card_type, location, owner, template_guid, state, attack, defense,
         name, cost, subtype, threshold, abilities, buffs, rarity, sockets, gems,
         original_guid, card_attributes) = row_data[:19]
    else:
        (uid, card_type, location, owner, template_guid, state, attack, defense,
         name, cost, subtype, threshold, abilities, buffs, rarity, sockets, gems,
         original_guid) = row_data[:18]
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
            "tags": saved.get("tags", {}) or {},
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
            try:
                saved = json.loads(buffs or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                saved = {}
            if not isinstance(saved, dict):
                saved = {}
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
                    "int_attrs": saved.get("int_attrs", {}) or {},
                    "card_integer_variables": saved.get(
                        "card_integer_variables", {}) or {},
                    "card_abilities": json.loads(abilities or "[]")
                    if isinstance(abilities, str) else (abilities or []),
                    "parent_uid": _parent_uid(buffs)}
    return {"card_uid": int(source_uid or 0), "location": "champions",
            "user_id": int(controller_uid or 0),
            "owner_id": int(controller_uid or 0), "controller_id": int(controller_uid or 0),
            "card_type": "Champion", "attack": 0, "defense": 0}


def _parent_uid(serialized_buffs):
    try:
        value = json.loads(serialized_buffs or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return 0
    return int(value.get("parent_uid", 0) or 0) if isinstance(value, dict) else 0


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
    """Return the SessionCardId-backed uids of one authored target template.

    Projecting the candidate pool asks every candidate for its current
    threshold/subtype view, and that view is itself an aura scan.  Share one
    projection memo across the whole pool so each authored target set is
    resolved once per scan instead of once per candidate card.
    """
    from .static_rules import _projection_cache
    with _projection_cache():
        return _legal_targets(
            db, session_id, controller_uid, template_id, source_uid,
            both_players=both_players, champions=champions,
            battle_state=battle_state)


def _legal_targets(db, session_id, controller_uid, template_id, source_uid,
                   both_players=False, champions=None, battle_state=None):
    template = target_template(db, template_id)
    if not template:
        return []
    kind = _last(template.get("target_kind"))
    state = battle_state or {}
    if kind == "AbilitySourceCardTargetTemplate" and source_uid is not None:
        source_uid = int(source_uid)
        source_card = _source_card(db, session_id, source_uid, controller_uid)
        if (bool(_target_field(template_id, "m_ChoiceOverride", False)) and
                int((source_card or {}).get("parent_uid") or 0)):
            source_uid = int(source_card["parent_uid"])
            source_card = _source_card(db, session_id, source_uid,
                                       controller_uid)
        try:
            filter_json = json.loads(template.get("filter_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            filter_json = {}
        return ([int(source_uid)] if records_filter_matches(
            source_card, filter_json, source=source_card,
            context=_FilterContext(state), player=int(controller_uid or 0))
                else [])
    if kind == "AbilityTriggerCardTargetTemplate":
        selector = str(_target_field(
            template_id, "m_TriggerSelector", "TriggerSource") or
            "TriggerSource").rsplit(".", 1)[-1]
        key = ("resolving_trigger_target_uid" if selector == "TriggerTarget"
               else "resolving_trigger_source_uid" if selector == "TriggerSource"
               else None)
        trigger_uid = state.get(key) if key else None
        if trigger_uid is None:
            return []
        card = _source_card(db, session_id, int(trigger_uid), controller_uid)
        try:
            filter_json = json.loads(template.get("filter_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            filter_json = {}
        return ([int(trigger_uid)] if records_filter_matches(
            card, filter_json,
            source=_source_card(db, session_id, source_uid, controller_uid),
            context=_FilterContext(state), player=int(controller_uid or 0))
                else [])
    if kind == "TargetsAPlayerOrHisStuff":
        return _chain_action_targets(
            db, session_id, controller_uid, template, template_id,
            source_uid, state, champions)
    try:
        filter_json = json.loads(template["filter_json"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        filter_json = {}
    from pvp_db import db_target_candidate_rows
    top_n = _find_filter(filter_json, "TopNOfDeck")
    zones = template_zones(template)
    # MatchSecondaryTargetTemplate overrides the base enumerator and calls
    # Session.GetAllCards when its collection mask is None. The base target
    # enumerator instead offers no candidates for None.
    if kind == "MatchSecondaryTargetTemplate" and not zones:
        zones = ["champions", "warzone", "hand", "deck", "discard", "void",
                 "underground", "PlayedResources", "CastSpells",
                 "choosing", "mod", "simulacrum"]
    if top_n is not None:
        zones = ["deck"] if "deck" in zones else []
    if kind == "PlayerTargetTemplate":
        rows = []  # its C# enumerator visits champions only
    elif zones:
        rows = db_target_candidate_rows(
            session_id, zones, controller_uid=controller_uid,
            both_players=(True if kind == "MatchSecondaryTargetTemplate"
                          else both_players),
            top_n=top_n is not None, conn=db)
    else:
        rows = []
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
        if kind not in {"TargetsAPlayerOrHisStuff",
                        "MatchSecondaryTargetTemplate"} and self_only and \
                card["user_id"] != int(controller_uid or 0):
            continue
        if kind not in {"TargetsAPlayerOrHisStuff",
                        "MatchSecondaryTargetTemplate"} and opposing and \
                card["user_id"] == int(controller_uid or 0):
            continue
        # These derived enumerators do not run the base target's protections.
        # Duplicate applies only the spectral check; SharedName and
        # MatchSecondary enumerate from CardFilter and validate separately.
        if (kind not in {"MatchSecondaryTargetTemplate",
                         "SharedNameTargetTemplate",
                         "DuplicateCardTargetTemplate"} and
                _protected_target(db, session_id, battle_state, card, source,
                                  int(controller_uid or 0), is_auto)):
            continue
        if (kind in {"AbilityTargetTemplate", "DuplicateCardTargetTemplate"}
                and _is_spectral(db, session_id, battle_state, card, source,
                                 controller_uid)):
            continue
        cards.append(card)
        by_owner.setdefault(card["user_id"], []).append(card)
    global_pool_kinds = (
        "CompareAttackToLowestFilter", "CompareAttackToHighestFilter",
        "CompareDefenseToLowestFilter", "CompareHealthToLowestFilter",
        "CompareHealthToHighestFilter", "CompareResourceCostToHighestFilter",
        "CompareResourceCostToMyHighestFilter",
        "PlayersWhoControlMatchingFilter", "TopNOfDeck")
    use_global_pool = any(_find_filter(filter_json, item)
                          for item in global_pool_kinds)
    use_global_pool = use_global_pool or _requires_global_filter_pool(
        filter_json)
    global_cards = list(cards)
    if use_global_pool:
        all_zones = sorted(set(_ZONE_MAP.values()))
        global_rows = db_target_candidate_rows(
            session_id, all_zones, controller_uid=controller_uid,
            both_players=True, top_n=False, conn=db)
        global_cards = [_card(row, battle_state, db, session_id)
                        for row in global_rows]
        for champion in champions or ():
            try:
                from domain.enums import ECardCollections
                uid, owner = int(champion[0]), int(champion[1])
                global_cards.append({
                    "card_uid": uid, "card_type": "Champion",
                    "location": "champions", "user_id": owner,
                    "owner_id": owner, "controller_id": owner,
                    "name": champion[2] if len(champion) > 2 else "Champion",
                    "attack": 0,
                    "defense": int(champion[3] or 0)
                    if len(champion) > 3 else 0,
                    "collection": int(ECardCollections.Champions),
                })
            except (IndexError, TypeError, ValueError):
                continue
    # One template filter is evaluated against every candidate in the scanned
    # zones, so compile it once per (spec, candidate-pool) pair: rebuilding the
    # Records filter tree per candidate dominated target/static evaluation.
    # Both key objects are call-local, and the predicate keeps its evaluation
    # context (including the pool) alive, so ids cannot be recycled here.
    evaluators = {}

    def matches(card, spec, pool):
        key = (id(spec), id(pool))
        predicate = evaluators.get(key)
        if predicate is None:
            context = _FilterContext(battle_state or {})
            context["cards"] = global_cards if use_global_pool else pool
            context["all_cards"] = global_cards if use_global_pool else pool
            context["active_player_id"] = (battle_state or {}).get(
                "active_player_id", controller_uid)
            # The activating player is the authoritative ``player`` operand for
            # cost/target filters.  Relying only on the source-card projection
            # makes an optional or partially materialized source look
            # uncontrolled, which suppresses valid payment candidates.
            predicate = records_filter_evaluator(
                spec, source=source, context=context,
                player=int(controller_uid or 0))
            evaluators[key] = predicate
        return predicate(card)
    if top_n is not None:
        # TopNOfDeck is a CardFilter and can be nested in And/Or/Not. Evaluate
        # the complete Records tree against each card; the filter leaf owns
        # amount modifiers, bottom counting and spectral expansion.
        context = _FilterContext(battle_state or {})
        context["cards"] = global_cards
        context["all_cards"] = global_cards
        context["active_player_id"] = (battle_state or {}).get(
            "active_player_id", controller_uid)
        predicate = records_filter_evaluator(
            filter_json, source=source, context=context,
            player=int(controller_uid or 0))
        return [int(card["card_uid"]) for card in cards if predicate(card)]


    out = [int(card["card_uid"]) for card in cards
           if (kind == "TargetsAPlayerOrHisStuff" or
               matches(card, filter_json, cards))]
    if champions and (kind == "PlayerTargetTemplate" or
                       "champions" in zones):
        player_target = kind == "PlayerTargetTemplate"
        for uid, owner, name, health in champions:
            if kind == "MatchSecondaryTargetTemplate":
                player_allowed = True
            elif player_target:
                player_allowed = _player_filter_accepts(
                    player_filter, owner, controller_uid)
            else:
                player_allowed = ((both_players or owner == controller_uid) and
                                  (not self_only or owner == controller_uid) and
                                  (not opposing or owner != controller_uid))
            if not player_allowed:
                continue
            card = {"card_uid": int(uid), "card_type": "Champion",
                    "location": "champions", "user_id": owner,
                    "controller_id": owner, "name": name or "Champion",
                    "defense": int(health or 0), "attack": 0}
            # PlayerTargetTemplate overrides IsCardValidTarget in C# and
            # checks only champion identity, player filter and its own filter.
            if (not player_target and kind not in {
                    "MatchSecondaryTargetTemplate",
                    "SharedNameTargetTemplate"} and _protected_target(
                    db, session_id, battle_state, card, source,
                    int(controller_uid or 0), is_auto)):
                continue
            filter_source = card if player_target else source
            try:
                predicate = records_filter_evaluator(
                    filter_json, source=filter_source,
                    context=_FilterContext(battle_state or {}),
                    player=int(controller_uid or 0))
                is_match = predicate(card)
            except (TypeError, ValueError, KeyError):
                is_match = False
            if is_match:
                out.append(int(uid))
    if kind == "MatchSecondaryTargetTemplate":
        return out
    if kind == "SharedNameTargetTemplate":
        # The C# override returns no candidates when the authored minimum
        # TargetField is absent, even though the base minimum resolves to 0.
        if _target_field(template_id, "m_MinTargetCount") is None:
            return []
        minimum, _maximum = _resolved_target_counts(
            template_id, template, state)
        groups = {}
        for card in cards:
            uid = int(card["card_uid"])
            if uid in out:
                groups.setdefault(str(card.get("name") or "").lower(), []).append(uid)
        allowed = {uid for group in groups.values() if len(group) >= minimum
                    for uid in group}
        return [uid for uid in out if uid in allowed]
    if kind == "DuplicateCardTargetTemplate":
        matches_by_collection = {}
        legal_by_uid = set(out)
        for card in cards:
            if int(card["card_uid"]) not in legal_by_uid:
                continue
            key = (int(card.get("user_id") or 0),
                   str(card.get("location") or "").lower(),
                   str(card.get("name") or ""))
            matches_by_collection[key] = matches_by_collection.get(key, 0) + 1
        duplicate_uids = []
        for uid in out:
            card = next((candidate for candidate in cards
                         if int(candidate["card_uid"]) == uid), None)
            if card is None:
                continue
            key = (int(card.get("user_id") or 0),
                   str(card.get("location") or "").lower(),
                   str(card.get("name") or ""))
            if matches_by_collection.get(key, 0) > 1:
                duplicate_uids.append(uid)
        return duplicate_uids
    return out


def _resolved_target_counts(template_id, template, battle_state=None,
                            variables=None):
    """Resolve TargetField bounds, including null max and TargetVariable."""
    state = battle_state or {}
    if variables is None:
        variables = state.get("ability_variables") or {}
    spec = _target_spec(template_id)
    if spec is not None:
        minimum = int(spec.resolved_minimum(variables) or 0)
        maximum_field = _target_field(template_id, "m_MaxTargetCount")
        maximum = (2 ** 31 - 1 if maximum_field is None else
                   int(spec.resolved_maximum(variables) or 0))
        return max(0, minimum), max(0, maximum)
    minimum = int((template or {}).get("min_target_count") or 0)
    maximum = int((template or {}).get("max_target_count") or 0)
    return max(0, minimum), maximum if maximum else 2 ** 31 - 1


def filter_resolved_targets(db, session_id, controller_uid, template_id,
                            source_uid, selected, battle_state=None, *,
                            apply_collection=False):
    """Filter an already-resolved target set through a Records target.

    C# ``SecondaryTargetTemplate`` enumerates the previous target instance and
    applies only its CardFilter. Its PlayerFilter and CollectionFlags do not
    rescan or narrow that already-selected set.
    """
    template = target_template(db, template_id)
    if not template:
        return ()
    try:
        filter_json = json.loads(template.get("filter_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        filter_json = {}
    source = _source_card(db, session_id, source_uid, controller_uid)
    zones = set(template_zones(template)) if apply_collection else set()
    result = []
    for raw_uid in selected or ():
        try:
            uid = int(raw_uid)
        except (TypeError, ValueError):
            continue
        card = _source_card(db, session_id, uid, controller_uid)
        if not card:
            continue
        if zones and str(card.get("location") or "") not in zones:
            continue
        if records_filter_matches(
                card, filter_json, source=source,
                context=_FilterContext(battle_state or {}),
                player=int(controller_uid or 0)):
            result.append(uid)
    return tuple(dict.fromkeys(result))


def _chain_action_targets(db, session_id, controller_uid, template,
                          template_id, source_uid, state, champions=None):
    """Port ``TargetsAPlayerOrHisStuff`` against native projected chain data."""
    from pvp_db import db_target_candidate_rows
    try:
        filter_json = json.loads(template.get("filter_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        filter_json = {}
    # The client enumerates chain cards from the CastSpells collection
    # regardless of this template's advertised collection mask.
    zones = ["CastSpells"]
    rows = db_target_candidate_rows(
        session_id, zones, controller_uid=controller_uid, both_players=True,
        conn=db)
    descriptors = [item for item in state.get("stack", ())
                   if isinstance(item, dict)]
    champ_owners = {}
    for owner, uid in (state.get("champ_map") or {}).items():
        try:
            champ_owners[int(uid)] = int(owner)
        except (TypeError, ValueError):
            continue
    for item in champions or ():
        try:
            uid, owner = int(item[0]), int(item[1])
            champ_owners[uid] = owner
        except (IndexError, TypeError, ValueError):
            continue
    player_filter = str(template.get("player_filter") or "").lower()
    result = []
    for row in rows:
        spell = _card(row, state, db, session_id)
        spell_uid = int(spell["card_uid"])
        descriptor = next((item for item in descriptors
                           if int(item.get("source_uid") or 0) == spell_uid and
                           str(item.get("kind") or "").lower() in
                           {"spell", "troop", "artifact"}), None)
        if descriptor is None:
            continue
        activations = []
        activation = descriptor.get("activation_data") or {}
        if isinstance(activation, dict):
            activations.append(activation)
        # Native PvP card-play descriptors keep one activation per ability;
        # C# exposes their flattened TargetMap to this target template.
        nested = descriptor.get("activations") or {}
        if isinstance(nested, dict):
            activations.extend(value for value in nested.values()
                               if isinstance(value, dict))
        elif isinstance(nested, (list, tuple)):
            activations.extend(value for value in nested
                               if isinstance(value, dict))
        target_map = {}
        for entry in activations:
            target_map.update(entry.get("target_map") or {})
        target_values = []
        for selected in target_map.values():
            if isinstance(selected, dict):
                selected = selected.get("value", selected.get("uid64", selected))
            if isinstance(selected, (tuple, list, set)):
                target_values.extend(selected)
            else:
                target_values.append(selected)
        for value in target_values:
            try:
                target_uid = int(value)
            except (TypeError, ValueError):
                continue
            card = None
            target_owner = champ_owners.get(target_uid)
            if target_owner is None:
                card = _source_card(db, session_id, target_uid, controller_uid)
                target_owner = int(card.get("user_id") or 0) if card else None
            if target_owner is None:
                continue
            if player_filter in {"self", "you", "controller"} and \
                    target_owner != int(controller_uid or 0):
                continue
            if player_filter in {"opponent", "opposing", "singleopponent",
                                 "multipleopponents"} and \
                    target_owner == int(controller_uid or 0):
                continue
            if target_uid in champ_owners:
                result.append(spell_uid)
                break
            if card is not None and records_filter_matches(
                    card, filter_json, source=spell,
                    context=_FilterContext(state),
                    player=int(controller_uid or 0)):
                result.append(spell_uid)
                break
    return result


def _shared_name_card_is_valid(db, session_id, card, template_id, template,
                               battle_state, controller_uid, champions,
                               variables=None):
    """Mirror SharedNameTargetTemplate.IsCardValidTarget's class override."""
    if _target_field(template_id, "m_MinTargetCount") is None:
        return False
    minimum, _maximum = _resolved_target_counts(
        template_id, template, battle_state, variables)
    zones = template_zones(template)
    if not zones:
        return minimum <= 0
    owner = _target_owner(card)
    player_filter = _last(template.get("player_filter")).lower()
    rows = []
    from pvp_db import db_target_candidate_rows
    rows.extend(db_target_candidate_rows(
        session_id, zones, controller_uid=controller_uid,
        both_players=True, conn=db))
    name = str(card.get("name") or "").lower()
    matching = 0
    for row in rows:
        candidate = _card(row, battle_state, db, session_id)
        candidate_owner = _target_owner(candidate)
        if player_filter in {"self", "you", "controller"}:
            if candidate_owner != owner:
                continue
        elif player_filter in {"singleopponent", "multipleopponents"}:
            # The C# SharedName per-card override excludes the selected
            # target's controller here (the unlike-named base target check
            # is retained exactly as implemented by the client).
            if candidate_owner == owner:
                continue
        if str(candidate.get("name") or "").lower() == name:
            matching += 1
    if "champions" in {zone.lower() for zone in zones}:
        for entry in champions or ():
            try:
                uid, candidate_owner = int(entry[0]), int(entry[1])
                candidate_name = str(entry[2] if len(entry) > 2 else
                                      "Champion").lower()
            except (IndexError, TypeError, ValueError):
                continue
            if player_filter in {"self", "you", "controller"} and \
                    candidate_owner != owner:
                continue
            if player_filter in {"singleopponent", "multipleopponents"} and \
                    candidate_owner == owner:
                continue
            if candidate_name == name:
                matching += 1
    return matching >= minimum


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
                              champions=None, battle_state=None,
                              variables=None):
    """Validate a submitted TargetInstance like C# IsTargetValid.

    Target enumeration and target validation are deliberately separate. Some
    C# subclasses enumerate a constrained choice but validate against their
    own override, while the base class's None collection mask offers no
    options yet accepts a directly supplied card in any collection.
    """
    template = target_template(db, template_id)
    values = selected if isinstance(selected, (list, tuple, set)) else [selected]
    try:
        values = [int(getattr(v, "uid64", v)) for v in values if v is not None]
    except (TypeError, ValueError):
        return []
    if not template:
        return values
    minimum, maximum = _resolved_target_counts(
        template_id, template, battle_state, variables)
    if len(values) > maximum:
        return []
    if not values and template.get("optional"):
        return []
    if len(values) < minimum:
        if not template.get("allow_best_effort_minimum"):
            return []
        legal_values = legal_targets(
            db, session_id, controller_uid, template_id, source_uid,
            both_players=both_players, champions=champions,
            battle_state=battle_state)
        # Client ValidateMinimumTargetCount accepts a short target only when
        # it contains every target the client itself considered legal.
        if (len(legal_values) > len(values) or any(
                value not in values for value in legal_values)):
            return []

    kind = _last(template.get("target_kind"))
    state = battle_state or {}
    if values:
        actual = [_special_target_card(
            db, session_id, uid, controller_uid, state, champions)
                  for uid in values]
        if any(card is None for card in actual):
            return []
        source = _source_card(db, session_id, source_uid, controller_uid)
        try:
            filter_json = json.loads(template.get("filter_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            filter_json = {}
        source_owner = _target_owner(source or {})
        player_filter = template.get("player_filter")
        if kind == "AbilitySourceCardTargetTemplate":
            expected_uid = int(source_uid or 0)
            if (bool(_target_field(template_id, "m_ChoiceOverride", False)) and
                    int((source or {}).get("parent_uid") or 0)):
                expected_uid = int(source["parent_uid"])
                source = _source_card(db, session_id, expected_uid,
                                      controller_uid)
            if len(values) != 1 or values[0] != expected_uid:
                return []
            if not records_filter_matches(
                    source, filter_json, source=source,
                    context=_FilterContext(state),
                    player=int(controller_uid or 0)):
                return []
        elif kind == "PlayerTargetTemplate":
            for card in actual:
                if str(card.get("card_type") or "").lower() != "champion":
                    return []
                if not _player_filter_accepts(
                        player_filter, _target_owner(card), controller_uid):
                    return []
                if not records_filter_matches(
                        card, filter_json, source=card,
                        context=_FilterContext(state),
                        player=int(controller_uid or 0)):
                    return []
        elif kind == "SharedNameTargetTemplate":
            if any(not _shared_name_card_is_valid(
                    db, session_id, card, template_id, template, state,
                    controller_uid, champions, variables) for card in actual):
                return []
        elif kind == "TargetsAPlayerOrHisStuff":
            available = set(_chain_action_targets(
                db, session_id, controller_uid, template, template_id,
                source_uid, state, champions))
            if any(uid not in available for uid in values):
                return []
        else:
            for card in actual:
                owner = _target_owner(card)
                if not _player_filter_accepts(
                        player_filter, owner, controller_uid):
                    return []
                if not _target_in_collection(card, template):
                    return []
                if kind == "DuplicateCardTargetTemplate":
                    if _is_spectral(db, session_id, state, card, source,
                                    controller_uid):
                        return []
                    if not records_filter_matches(
                            card, filter_json, source=source,
                            context=_FilterContext(state),
                            player=int(controller_uid or 0)):
                        return []
                    from pvp_db import db_target_candidate_rows
                    siblings = db_target_candidate_rows(
                        session_id, [card.get("location")],
                        controller_uid=owner, both_players=False, conn=db)
                    same_name = any(
                        int(row[0]) != int(card.get("card_uid") or 0) and
                        str(row[8] or "") == str(card.get("name") or "")
                        for row in siblings)
                    if not same_name:
                        return []
                    continue

                # The base IsCardValidTarget protection path keys the
                # SpellShield exception to the source card's controller, but
                # TargetingImmunity is checked against the responsible player.
                in_protected_zone = str(card.get("location") or "").lower() \
                    in {"warzone", "champions"}
                if (not bool(template.get("is_auto_target")) and
                        in_protected_zone and owner != source_owner):
                    if int(card.get("attributes", 0) or 0) & _SPELL_SHIELD_ATTR:
                        return []
                    from .stealth import champion_target_is_spellshielded
                    if champion_target_is_spellshielded(state, card):
                        return []
                if (not bool(template.get("is_auto_target")) and
                        in_protected_zone and owner != int(controller_uid or 0)
                        and _targeting_immune(
                            db, session_id, state, card, source)):
                    return []
                if _is_spectral(db, session_id, state, card, source,
                                controller_uid):
                    return []
                if not records_filter_matches(
                        card, filter_json, source=source,
                        context=_FilterContext(state),
                        player=int(controller_uid or 0)):
                    return []

        if kind == "SharedNameTargetTemplate":
            names = [str(card.get("name") or "").lower() for card in actual]
            if names and any(name != names[0] for name in names[1:]):
                return []

        # C# stores the distinct controlling players separately on the
        # TargetInstance. Derive that set from authoritative selected cards.
        owners = {_target_owner(card) for card in actual}
        pf = str(player_filter or "").rsplit(".", 1)[-1].lower()
        responsible = int(controller_uid or 0)
        if kind != "AbilitySourceCardTargetTemplate":
            if pf in {"self", "you", "controller"} and owners != {responsible}:
                return []
            if pf == "singleopponent" and (
                    len(owners) != 1 or responsible in owners):
                return []
            if pf == "singleplayer" and len(owners) != 1:
                return []
            if pf == "multipleopponents" and (
                    not owners or responsible in owners):
                return []
            if pf in {"multipleplayers", "allplayers"} and not owners:
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
        # Some authored sacrifice target filters say only "a troop you
        # control".  For a deploy sacrifice, the source is not an eligible
        # replacement for the optional "another troop" choice.
        if str(_effect_type) == "SacrificeCardAbilityEffectTemplate":
            candidates = [uid for uid in candidates
                          if int(uid) != int(source_uid or 0)]
        if candidates:
            return candidates[0]
    return None
