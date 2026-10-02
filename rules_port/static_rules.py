"""RulesPort-owned card stat projection.

The mutable instance/template fields are authoritative runtime facts.  The
continuous-ability scan uses the native typed modifier evaluator; unsupported
metadata is reported as an explicit failure rather than delegated to the old
evaluator, so an attached session cannot silently become hybrid.
"""

from __future__ import annotations

import ast
import json
import math
import threading
from contextlib import contextmanager

import game_engine


def _empty_deltas():
    return {"atk": 0, "def": 0, "cost_mod": 0, "attrs": 0,
            "flags": set(), "rage": 0, "gladiator": 0, "rules": [],
            "card_properties": {}}


_EMPTY_TARGET_SET = frozenset()
_MISSING = object()
# Continuous leaf properties that change a card's projected view (thresholds
# and subtype) rather than its combat numbers.
_CARD_PROPERTY_LEAVES = frozenset({"cardthreshold", "subtype"})
# IntAttr leaves that constrain champions themselves rather than a card's
# combat numbers.  Champions are synthetic SessionCardIds without a
# ``game_cards`` row, so a champion-targeted leaf of this shape is aggregated
# by :func:`controller_flags` instead of by the per-card projection.
_CHAMPION_INTATTR_FLAGS = {
    "cantgainhealth": "cant_gain_health",
    "cantlosehealth": "cant_lose_health",
    "cantplaycards": "cant_play_cards",
    "unlimitedhandsize": "no_max_hand_size",
}


class _ProjectionCache:
    """Memo of the target-independent inputs of one static projection.

    A single ``effective_stats``/``effective_cost``/``effective_attributes``
    call re-derives the same continuous modifiers many times: every projected
    target card asks every source card for its authored leaves, every aura
    leaf asks its ability for the authored target template, and every
    candidate card in that template's pool projects its own threshold/subtype
    view.  Those inputs cannot change while one synchronous projection runs,
    so they are memoized here for the lifetime of the outermost scan.
    """

    __slots__ = ("source_cards", "source_abilities", "ability_leaves",
                 "ability_targets", "target_sets", "card_property_sources",
                 "card_rows", "scanning")

    def __init__(self):
        self.source_cards = {}
        self.source_abilities = {}
        self.ability_leaves = {}
        self.ability_targets = {}
        self.target_sets = {}
        self.card_property_sources = {}
        self.card_rows = {}
        # Cards whose static scan is already on the stack.  A "for each ..."
        # variable can re-enter effective_stats for a card inside its own
        # projection; the client treats that self-reference as no further
        # delta, and without the guard the scan recursed until it crashed.
        self.scanning = set()


_projection_local = threading.local()


@contextmanager
def _projection_cache():
    """Share one :class:`_ProjectionCache` with every nested scan."""
    cache = getattr(_projection_local, "cache", None)
    if cache is not None:
        yield cache
        return
    cache = _ProjectionCache()
    _projection_local.cache = cache
    try:
        yield cache
    finally:
        _projection_local.cache = None


def _static_abilities(db, session_id, card_uid, cache=None):
    """Return the continuous abilities authored on one materialized card."""
    from pvp_db import db_card_ability_payload, db_ability_static_metadata
    key = (int(session_id or 0), int(card_uid))
    if cache is not None:
        cached = cache.source_abilities.get(key)
        if cached is not None:
            return cached
    payload = db_card_ability_payload(session_id, int(card_uid), conn=db)
    try:
        values = json.loads(payload or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        values = []
    result = []
    for value in values:
        guid = str(value).lower()
        metadata = db_ability_static_metadata(guid, conn=db)
        if not metadata:
            continue
        if not metadata[0] and not metadata[1]:
            result.append(guid)
            continue
        raw = str(metadata[2] or "")
        if ("CardCreatedEvent" in str(metadata[0] or "") and
                all(zone in raw for zone in ("Deck", "Hand", "Warzone", "Discard"))):
            result.append(guid)
    result = tuple(result)
    if cache is not None:
        cache.source_abilities[key] = result
    return result


def _static_leaves(db, ability_guid, cache=None):
    """Return the continuous CardModifier leaves authored on one ability."""
    from .metadata import modifier_metadata
    from pvp_db import db_ability_effect_rows
    cache_key = str(ability_guid or "").lower()
    if cache is not None:
        cached = cache.ability_leaves.get(cache_key)
        if cached is not None:
            return cached
    leaves = []
    for effect_guid, effect_type, raw_param in db_ability_effect_rows(
            ability_guid, conn=db):
        if effect_type != "CardModifierAbilityEffectTemplate":
            continue
        try:
            param = json.loads(raw_param or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(param, dict) or param.get("duration") not in (
                "WhileCardInPlay", "Permanent", "BeginningOfOwnersTurn"):
            continue
        typed = modifier_metadata(effect_guid)
        if typed:
            param = dict(param)
            for key, value in typed.items():
                if value not in (None, ""):
                    if key == "value" and not param.get("amount"):
                        param["amount"] = value
                    else:
                        param.setdefault(key, value)
        from pvp_db import db_ability_raw_json
        leaves.append((param, db_ability_raw_json(ability_guid, conn=db) or ""))
    leaves = tuple(leaves)
    if cache is not None:
        cache.ability_leaves[cache_key] = leaves
    return leaves


def _literal_leaf(param, *, extra_properties=()):
    """Return a native literal leaf, or ``None`` for a dynamic leaf."""
    prop = str(param.get("property") or "").lower()
    if prop not in {"attack", "defense", "cardcost", "attribute", "intattr",
                    "damagemultiplier", *extra_properties}:
        return None
    if param.get("input_variable"):
        return None
    value = param.get("amount")
    if value in (None, ""):
        value = param.get("input_value", param.get("value", 0))
    try:
        value = int(value or 0)
    except (TypeError, ValueError):
        return None
    return prop, value


def _variable_card_candidates(db, session_id, battle_state, source_uid,
                               owner, variable, *, self_owner="responsible"):
    """Apply a Records variable's player, zone and card filters like C#.

    MultipleOpponents enumerates only players other than the variable's
    responsible/source controller. MultiplePlayers enumerates every player.
    The C# count, sum, highest-card and counter variables explicitly skip
    SingleOpponent and SinglePlayer because a scalar variable cannot choose
    one player from those filters.
    """
    from pvp_db import db_target_candidate_rows
    from .targeting import _card, _source_card
    from .filters import records_filter_matches
    zone_map = {"Deck": "deck", "Hand": "hand", "Warzone": "warzone",
                "Crypt": "discard", "Discard": "discard",
                "Void": "void", "CastSpells": "CastSpells",
                "PlayedResources": "PlayedResources", "Choosing": "choosing",
                "Underground": "underground"}
    zones = [zone_map.get(value, str(value).lower()) for value in
             str(variable.get("m_CollectionFlags") or "").split("|") if value]
    if not zones or all(str(zone).lower() == "none" for zone in zones):
        return None
    player_filter = str(variable.get("m_PlayerFilter") or "Unknown").lower()
    if player_filter == "unknown":
        return None
    if player_filter in {"singleopponent", "singleplayer"}:
        return None

    source = _source_card(db, session_id, int(source_uid), int(owner))
    source_owner = int((source or {}).get("controller_id", owner) or 0)
    scope_owner = (source_owner if self_owner == "source" else int(owner))
    both_players = player_filter in {
        "multipleplayers", "allplayers", "multipleopponents"}
    rows = db_target_candidate_rows(
        session_id, zones, controller_uid=scope_owner,
        both_players=both_players, conn=db)
    if player_filter == "multipleopponents":
        rows = [row for row in rows if int(row[3] or 0) != scope_owner]
    cards = [_card(row) for row in rows]
    spec = variable.get("m_CardFilter") or {}
    context = dict(battle_state or {}, cards=cards)
    return [card for card in cards if records_filter_matches(
        card, spec, source=source, context=context, player=int(owner))]


def _variable_record(raw, variable_name, kind):
    try:
        record = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    variable = next((item for item in record.get("m_Variables", [])
                     if item.get("m_Name") == variable_name), None)
    if not variable or str(variable.get("_t", "")).rsplit(".", 1)[-1] != kind:
        return None
    return variable


def _count_variable(db, session_id, battle_state, source_uid, owner, raw,
                    variable_name):
    """Evaluate a typed CardCountAbilityVariable using native filters."""
    variable = _variable_record(raw, variable_name,
                                "CardCountAbilityVariable")
    if variable is None:
        return None
    cards = _variable_card_candidates(
        db, session_id, battle_state, source_uid, owner, variable)
    if cards is None:
        return int(variable.get("m_DefaultValue", 0) or 0)
    faction = str(variable.get(
        "m_OnlyIncludeDifferentRacesForFaction", "Unknown") or
        "Unknown").rsplit(".", 1)[-1].lower()
    races_by_faction = {
        "aria": ("human", "elf", "coyotle", "orc"),
        "underworld": ("dwarf", "shin'hare", "vennen", "necrotic"),
    }
    if faction != "unknown":
        race_names = races_by_faction.get(faction, ())
        # Client CardCountAbilityVariable walks ERace values and counts each
        # matching faction race once when that race name is a subtype of a
        # filtered card.  Card.HasSubType splits CurrentSubtype on spaces.
        races = set()
        for card in cards:
            subtype = str(card.get("subtype") or "").strip().lower()
            if not subtype:
                continue
            subtypes = set(subtype.split(" "))
            races.update(race for race in race_names if race in subtypes)
        return len(races)
    return len(cards)


def _sum_variable(db, session_id, battle_state, source_uid, owner, raw,
                  variable_name):
    """Evaluate a typed CardSumAbilityVariable using native card facts."""
    variable = _variable_record(raw, variable_name, "CardSumAbilityVariable")
    if variable is None:
        return None
    cards = _variable_card_candidates(
        db, session_id, battle_state, source_uid, owner, variable,
        self_owner="source")
    if cards is None:
        return int(variable.get("m_DefaultValue", 0) or 0)
    prop = str(variable.get("m_Property") or "").lower()
    if prop in {"currentattackvalue", "attack", "cardattack"}:
        return sum(effective_stats(db, session_id, battle_state,
                                   int(card["card_uid"]))[0]
                   for card in cards)
    if prop in {"currentdefensevalue", "defense", "carddefense"}:
        return sum(effective_stats(db, session_id, battle_state,
                                   int(card["card_uid"]))[1]
                   for card in cards)
    if prop in {"resourcecosttrue", "resourcecost", "cost", "cardcost"}:
        return sum(effective_cost(
            db, session_id, battle_state, int(card["card_uid"]))
                   for card in cards)
    return None


def _highest_card_variable(db, session_id, battle_state, source_uid, owner,
                           raw, variable_name):
    """Evaluate a typed HighestCardAbilityVariable from current card facts."""
    variable = _variable_record(raw, variable_name,
                                "HighestCardAbilityVariable")
    if variable is None:
        return None
    cards = _variable_card_candidates(
        db, session_id, battle_state, source_uid, owner, variable,
        self_owner="source")
    if cards is None:
        return int(variable.get("m_DefaultValue", 0) or 0)
    if not cards:
        return -2147483648
    prop = str(variable.get("m_Property") or "").lower()
    if prop in {"currentattackvalue", "attack", "cardattack"}:
        values = [effective_stats(db, session_id, battle_state,
                                  int(card["card_uid"]))[0]
                  for card in cards]
    elif prop in {"currentdefensevalue", "defense", "carddefense"}:
        values = [effective_stats(db, session_id, battle_state,
                                  int(card["card_uid"]))[1]
                  for card in cards]
    elif prop in {"resourcecosttrue", "resourcecost", "cost", "cardcost"}:
        values = [effective_cost(db, session_id, battle_state,
                                 int(card["card_uid"])) for card in cards]
    else:
        return None
    return max(values) if values else -2147483648


def _counter_variable(db, session_id, battle_state, source_uid, owner, raw,
                      variable_name):
    """Evaluate a typed CounterVariable from native persisted counters."""
    variable = _variable_record(raw, variable_name, "CounterVariable")
    if variable is None:
        return None
    cards = _variable_card_candidates(
        db, session_id, battle_state, source_uid, owner, variable)
    if cards is None:
        return int(variable.get("m_DefaultValue", 0) or 0)
    wanted = str((variable.get("m_CardCounterTemplateId") or {}).get(
        "m_Guid") or "").lower()
    total = 0
    for card in cards:
        for name, value in (card.get("counters") or {}).items():
            guid = str((card.get("counter_guids") or {}).get(name, "")).lower()
            if wanted and guid != wanted:
                continue
            value = int(value or 0)
            if variable.get("m_UseHighestValue"):
                total = max(total, value)
            else:
                total += value
    return total


def _list_card_uids(db, session_id, state, list_name, source_uid,
                    *, pull_source=False, owner=None):
    """Read a client TAC list from the active ability or source card state."""
    state = state if isinstance(state, dict) else {}
    ability_guid = str(state.get("resolving_ability") or "").lower()
    path = [part for part in str(list_name or "").split(">") if part]
    leaf_name = path[-1] if path else str(list_name or "")
    candidates = None
    scoped_to_champions = bool(
        path and path[0].lower().startswith(("you", "all")))
    if scoped_to_champions:
        scope = ("CardStatsThisTurn" if len(path) > 1 and
                 path[1].startswith("CardStatsThisTurn") else "CardGameStats")
        champion_map = state.get("champ_map") or {}
        if path[0].lower().startswith("you"):
            champion_uid = _champion_uid(state, owner)
            champion_uids = (champion_uid,) if champion_uid is not None else ()
        else:
            champion_uids = tuple(dict.fromkeys(
                int(uid) for uid in champion_map.values() if uid is not None))
        from .statistics import tac_list
        candidates = tuple(value for champion_uid in champion_uids
                           for value in tac_list(
                               state, "cards", champion_uid, scope,
                               leaf_name))
        list_name = leaf_name
    if not scoped_to_champions and pull_source and source_uid is not None:
        from pvp_db import db_card_mutation_field
        try:
            raw = db_card_mutation_field(
                session_id, int(source_uid), "permanent_buffs", conn=db)
            source_data = json.loads(raw or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            source_data = {}
        if isinstance(source_data, dict):
            permanent = source_data.get("permanent_data") or {}
            if isinstance(permanent, dict):
                if leaf_name in permanent:
                    candidates = permanent[leaf_name]
            if candidates is None:
                source_lists = source_data.get("list_attrs") or {}
                if leaf_name in source_lists:
                    candidates = source_lists[leaf_name]
            if candidates is None:
                # The C# pull-source path calls GetOrCreate, so a valid
                # source card with no prior entries has an empty list.
                candidates = ()
    if not scoped_to_champions and candidates is None:
        lists = state.get("ability_lists") or {}
        current = lists.get(ability_guid, {}) if ability_guid else {}
        if isinstance(current, dict) and leaf_name in current:
            candidates = current[leaf_name]
        elif leaf_name in lists:
            candidates = lists[leaf_name]
    if not scoped_to_champions and candidates is None:
        all_lists = state.get("list_attrs") or {}
        current = all_lists.get(ability_guid, {}) if ability_guid else {}
        if isinstance(current, dict) and leaf_name in current:
            candidates = current[leaf_name]
    if (not scoped_to_champions and candidates is None and
            leaf_name in {"StoredTargets", "stored_targets"}):
        for key in ("stored_targets", "stored_targets_this_turn"):
            stored_map = state.get(key) or {}
            if ability_guid in stored_map:
                candidates = stored_map[ability_guid]
                break
    if candidates is None:
        return None
    result = []
    for value in candidates if isinstance(candidates, (list, tuple, set)) else ():
        if isinstance(value, dict):
            stored_name = str(value.get("name") or "").lower()
            if stored_name in {"id", "cardid", "card_id"}:
                value = value.get("value")
            else:
                value = next((value[key] for key in
                              ("card_uid", "uid", "source_uid", "Id", "id",
                               "card_id", "CardId")
                              if value.get(key) is not None), None)
        try:
            if value is not None:
                result.append(int(value))
        except (TypeError, ValueError):
            continue
    return tuple(result)


def _card_property_value(db, session_id, state, uid, prop):
    """Evaluate the ECardProperties used by current list-sum records."""
    prop = str(prop or "").lower()
    if prop in {"currentattackvalue", "attack", "cardattack"}:
        return effective_stats(db, session_id, state, int(uid))[0]
    if prop in {"currentdefensevalue", "defense", "carddefense",
                "currenthealthvalue"}:
        return effective_stats(db, session_id, state, int(uid))[1]
    if prop in {"resourcecosttrue", "resourcecost", "cost", "cardcost"}:
        value = effective_cost(db, session_id, state, int(uid))
        from pvp_db import db_card_location
        if str(db_card_location(session_id, int(uid), conn=db) or "").lower() == "castspells":
            paid = ((state.get("card_x_cost_paid") or {}).get(
                str(int(uid)), (state.get("card_x_cost_paid") or {}).get(
                    int(uid), 0)))
            value += int(paid or 0)
        return value
    return 0


def _list_sum_variable(db, session_id, battle_state, raw, variable):
    """Sum current card properties from the typed ability list and filter."""
    list_name = variable.get("m_ListAttrName") or variable.get("m_Name")
    values = _list_card_uids(
        db, session_id, battle_state, list_name,
        (battle_state or {}).get("resolving_source_uid"),
        owner=(battle_state or {}).get("resolving_owner_id"))
    if values is None:
        return int(variable.get("m_DefaultValue", 0) or 0)
    prop = str(variable.get("m_Property") or "")
    from .targeting import _source_card
    from .filters import records_filter_matches
    source_uid = (battle_state or {}).get("resolving_source_uid")
    owner = int((battle_state or {}).get("resolving_owner_id", 0) or 0)
    source = (_source_card(db, session_id, int(source_uid), owner)
              if source_uid is not None else None)
    total = 0
    filter_spec = variable.get("m_CardFilter")
    for uid in values:
        row = _source_card(db, session_id, uid, owner)
        if not row:
            continue
        card = dict(row)
        card.setdefault("card_uid", uid)
        if filter_spec and not records_filter_matches(
                card, filter_spec, source=source,
                context=dict(battle_state or {}, cards=[card]), player=owner):
            continue
        total += int(_card_property_value(
            db, session_id, battle_state or {}, uid, prop) or 0)
    return total


def _count_list_variable(db, session_id, battle_state, source_uid, owner,
                         variable, variable_name):
    """Evaluate CountListAttr with the same source and card-filter rules."""
    list_name = variable.get("m_ListAttrName") or variable_name
    values = _list_card_uids(
        db, session_id, battle_state, list_name, source_uid,
        pull_source=bool(variable.get("m_PullFromSourceCard")),
        owner=owner)
    if values is None:
        return int(variable.get("m_DefaultValue", 0) or 0)
    filter_spec = variable.get("m_CardFilter")
    if not filter_spec:
        return len(values)
    from .targeting import _source_card
    from .filters import records_filter_matches
    source = (_source_card(db, session_id, int(source_uid), int(owner))
              if source_uid is not None else None)
    count = 0
    for uid in values:
        row = _source_card(db, session_id, uid, int(owner))
        if not row:
            continue
        card = dict(row)
        card.setdefault("card_uid", uid)
        if records_filter_matches(
                card, filter_spec, source=source,
                context=dict(battle_state or {}, cards=[card]),
                player=int(owner or 0)):
            count += 1
    return count


def _source_player_shards(db, session_id, owner, variable):
    """Count distinct authored shard bits in the variable's card zone."""
    from pvp_db import db_target_candidate_rows
    from .targeting import _card
    zone_map = {"Deck": "deck", "Hand": "hand", "Warzone": "warzone",
                "Crypt": "discard", "Discard": "discard", "Void": "void",
                "CastSpells": "CastSpells", "PlayedResources": "PlayedResources",
                "Choosing": "choosing", "Underground": "underground"}
    zones = [zone_map.get(value, str(value).lower()) for value in
             str(variable.get("m_Zone") or "Warzone").split("|") if value]
    bits = set()
    for row in db_target_candidate_rows(
            session_id, zones, controller_uid=int(owner),
            both_players=False, conn=db):
        card = _card(row)
        bits.update(int(value) for value in card.get("shards", ()) if value)
    return len(bits) if variable.get("m_DifferentThresholds") else 0


def _source_player_side(state, owner):
    """Map a responsible player ID to the shared player/AI view."""
    state = state if isinstance(state, dict) else {}
    if state.get("pvp"):
        pids = [int(pid) for pid in (state.get("pids") or ())]
        if pids:
            return "player" if int(owner or 0) == pids[0] else "ai"
    return "player" if int(owner or 0) else "ai"


def _champion_uid(state, owner):
    mapping = (state.get("champ_map") or {}) if isinstance(state, dict) else {}
    return mapping.get(int(owner or 0), mapping.get(str(int(owner or 0))))


def _source_player_threshold(state, owner, variable):
    """Evaluate threshold bit masks, including Any and combined shards."""
    import game_engine
    state = state if isinstance(state, dict) else {}
    side = _source_player_side(state, owner)
    thresholds = state.get(f"{side}_threshold") or {}
    if state.get("pvp") and f"thresh_{int(owner)}" in state:
        thresholds = state.get(f"thresh_{int(owner)}") or {}
    raw = str(variable.get("m_Threshold") or "Unknown")
    names = [part.rsplit(".", 1)[-1].strip().lower()
             for part in raw.split("|") if part.strip()]
    if any(name in {"any", "anycolor", "all"} for name in names):
        flags = {int(game_engine.SHARD_TO_FLAG[name])
                 for name in ("blood", "ruby", "sapphire", "wild", "diamond")
                 if name in game_engine.SHARD_TO_FLAG}
    else:
        flags = {int(game_engine.SHARD_TO_FLAG[name]) for name in names
                 if name in game_engine.SHARD_TO_FLAG}
    count_uniques = bool(variable.get("m_CountUniques"))
    total = 0
    for flag in flags:
        value = int(thresholds.get(flag, thresholds.get(str(flag), 0)) or 0)
        if value > 0:
            total += 1 if count_uniques else value
    return total


def _champion_tac_value(state, owner, scope, leaf,
                        default: int | None = 0):
    """Read one champion card or player TAC value from the RulesPort view."""
    uid = _champion_uid(state, owner)
    value = None
    if uid is not None:
        # C# keeps PlayerStatsThisTurn and PlayerGameStats as TAC scopes on
        # Player.m_ChampionCard. They are not stored on a separate Player TAC.
        value = _tac_stat(state, "cards", uid, scope, leaf)
        if value is None and str(scope).startswith("Player"):
            # Read checkpoints written by the earlier owner-keyed projection.
            value = _tac_stat(state, "players", owner, scope, leaf)
    if value is not None:
        return value
    # Earlier checkpoints and native projections may still carry the flat
    # client-shaped champion/player attribute dictionaries.
    attrs = (state.get("champion_int_attrs") or {}).get(str(uid), {}) \
        if uid is not None else {}
    if isinstance(attrs, dict) and leaf in attrs:
        return int(attrs.get(leaf) or 0)
    scoped = state.get("int_attr_values") or {}
    path = f"You>{scope}>{leaf}"
    try:
        return int(scoped.get(path, default) or 0)
    except (TypeError, ValueError):
        return int(default or 0)


def _tac_stat(state, collection, uid, scope, leaf):
    from .statistics import tac_stat
    return tac_stat(state, collection, uid, scope, leaf, default=None)


def _intattr_variable(db, session_id, battle_state, source_uid, owner,
                      variable_name, variable):
    """Read a typed IntAttrAbilityVariable from its authored TAC scope."""
    state = battle_state if isinstance(battle_state, dict) else {}
    values = state.get("ability_variables") or {}
    if variable_name in values:
        try:
            return int(values[variable_name] or 0)
        except (TypeError, ValueError):
            pass
    attribute = str(variable.get("m_IntAttrName") or "")
    default = int(variable.get("m_DefaultValue", 0) or 0)
    if not attribute:
        return default
    parts = [part for part in attribute.split(">") if part]
    leaf = parts[-1] if parts else attribute
    stored = state.get("int_attr_values") or {}
    if attribute in stored:
        try:
            return int(stored[attribute] or 0)
        except (TypeError, ValueError):
            pass
    scope_key = ">".join(parts[:-1])
    scoped = stored.get(scope_key, {}) if isinstance(stored, dict) else {}
    if isinstance(scoped, dict):
        scoped = scoped.get(str(int(owner or 0)), scoped)
        if isinstance(scoped, dict) and leaf in scoped:
            try:
                return int(scoped[leaf] or 0)
            except (TypeError, ValueError):
                pass

    from pvp_db import db_card_mutation_field

    source_value = None
    root = parts[0].lower() if parts else ""
    scope = parts[-2] if len(parts) > 1 else ""
    source_id = int(source_uid) if source_uid is not None else None
    from .statistics import ability_stat

    # IntAttrs written with AbilityInstance.Add live on the active instance;
    # PullFromSourceCard and explicit Card paths instead read source-card TAC.
    if root in {"damage dealt", "damagedealt"} or attribute == "DamageDealt":
        source_value = ability_stat(state, "DamageDealt", default=default)
    elif root in {"excessdamagedealt", "excess damage dealt"}:
        source_value = (_tac_stat(state, "cards", source_id,
                                  "CardStatsThisTurn", "ExcessDamageDealt")
                        if source_id is not None else None)
    elif attribute == "You>CardGameStats>ChargePointsGained":
        # This one C# variable deliberately follows the source's controller,
        # even when the responsible player changes during resolution.
        from pvp_db import db_card_owner_id
        source_owner = (db_card_owner_id(session_id, source_id, conn=db)
                        if source_id is not None else owner)
        champ_uid = _champion_uid(state, source_owner)
        source_value = (_tac_stat(state, "cards", champ_uid,
                                  "CardGameStats", "ChargePointsGained")
                        if champ_uid is not None else None)
    elif root == "you" and len(parts) > 2 and \
            parts[1].lower() == "permanentdata" and leaf in {
                "LearnSpellCount", "WarlordCount"}:
        guids = (state.get("talent_guids_by_owner") or {}).get(
            str(int(owner or 0)), ())
        from gamedata import DEFAULT_RECORD_STORE
        prefix = "Learn Spell:" if leaf == "LearnSpellCount" else "Warlord:"
        source_value = 0
        for guid in guids or ():
            talent = DEFAULT_RECORD_STORE.get(
                "ChampionTalentData", str(guid).lower())
            name = str(talent.field("m_Name", "") or "") if talent else ""
            if name.startswith(prefix):
                source_value += 1
    elif root.startswith("opposingchampions"):
        total = 0
        mapping = state.get("champ_map") or {}
        for participant, champion_uid in mapping.items():
            try:
                participant = int(participant)
            except (TypeError, ValueError):
                continue
            if participant == int(owner or 0):
                continue
            value = _tac_stat(state, "cards", champion_uid, scope, leaf)
            if value is None:
                value = int((state.get("int_attr_values") or {}).get(
                    f"OpposingChampions>{scope}>{leaf}", 0) or 0)
            total += int(value or 0)
        source_value = total
    elif root == "you":
        if len(parts) > 1 and parts[1].lower().startswith("currentcontext"):
            champion_uid = _champion_uid(state, owner)
            attrs = (state.get("champion_int_attrs") or {}).get(
                str(champion_uid), {}) if champion_uid is not None else {}
            source_value = attrs.get(leaf) if isinstance(attrs, dict) else None
        elif len(parts) > 2:
            source_value = _champion_tac_value(
                state, owner, scope, leaf, default=None)
    else:
        # An IntAttrAbilityVariable defaults to AbilityInstance TAC.  A
        # CardStatsThisTurn/CardGameStats path is the source card's TAC.
        card_scope = variable.get("m_PullFromSourceCard") or \
            root.startswith("cardstats") or root == "card"
        if card_scope and source_id is not None:
            card_scope_name = scope if scope else "CardStatsThisTurn"
            source_value = _tac_stat(
                state, "cards", source_id, card_scope_name, leaf)
            if source_value is None:
                for column in ("temporary_buffs", "permanent_buffs"):
                    try:
                        payload = json.loads(db_card_mutation_field(
                            session_id, source_id, column, conn=db) or "{}")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        payload = {}
                    attrs = payload.get("int_attrs", {}) if isinstance(
                        payload, dict) else {}
                    if isinstance(attrs, dict) and leaf in attrs:
                        source_value = int(attrs[leaf] or 0)
                        break
            if leaf.lower() == "rage" and source_value is None:
                try:
                    source_value = int(effective_stats(
                        db, session_id, state, source_id)[4] or 0)
                except (TypeError, ValueError, RuntimeError):
                    pass
        elif scope:
            source_value = ability_stat(state, leaf, default=None)
        else:
            source_value = ability_stat(state, leaf, default=None)

    if source_value is None:
        source_value = default
    try:
        result = int(source_value)
        return ((result + 1) // 2 if variable.get("m_HalfRoundedUp")
                else result)
    except (TypeError, ValueError):
        return default


def _expression_value(db, session_id, battle_state, source_uid, owner, raw,
                      variable_name, stack=None):
    """Resolve one native Records variable, including safe expressions."""
    stack = set(stack or ())
    if variable_name in stack:
        return None
    stack.add(variable_name)
    try:
        record = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    variable = next((item for item in record.get("m_Variables", [])
                     if item.get("m_Name") == variable_name), None)
    if not variable:
        return None
    kind = str(variable.get("_t", "")).rsplit(".", 1)[-1]
    if kind == "AbilityConstant":
        try:
            return int(variable.get("m_DefaultValue", 0) or 0)
        except (TypeError, ValueError):
            return 0
    if kind in {"CardCountAbilityVariable", "CardSumAbilityVariable",
                "CounterVariable"}:
        fn = {"CardCountAbilityVariable": _count_variable,
              "CardSumAbilityVariable": _sum_variable,
              "CounterVariable": _counter_variable}[kind]
        return fn(db, session_id, battle_state, source_uid, owner, raw,
                  variable_name)
    if kind == "HighestCardAbilityVariable":
        return _highest_card_variable(
            db, session_id, battle_state, source_uid, owner, raw,
            variable_name)
    if kind == "SourcePlayerChargeVariable":
        side = _source_player_side(battle_state, owner)
        state = battle_state or {}
        key = f"{side}_charges"
        if state.get("pvp") and f"chg_{int(owner)}" in state:
            key = f"chg_{int(owner)}"
        return int(state.get(key, variable.get("m_DefaultValue", 0)) or 0)
    if kind == "TriggerEventDamageProperty":
        state = battle_state or {}
        event_type = str(state.get("resolving_trigger_event_type") or "")
        if event_type.rsplit(".", 1)[-1] not in {
                "CardDealtDamageEvent", "CardWouldDealDamageEvent",
                "CardWouldBeDamagedEvent", "CardDamagedEvent"}:
            return int(variable.get("m_DefaultValue", 0) or 0)
        event_data = state.get("resolving_trigger_event_data") or {}
        event_tac = event_data.get("event_tac", event_data)
        try:
            return int(event_tac.get(
                "damage", event_tac.get("Damage", variable.get(
                    "m_DefaultValue", 0))) or 0)
        except (AttributeError, TypeError, ValueError):
            return int(variable.get("m_DefaultValue", 0) or 0)
    if kind == "SourcePlayerHealthVariable":
        side = _source_player_side(battle_state, owner)
        key = f"{side}_health"
        if (battle_state or {}).get("pvp"):
            state = battle_state or {}
            raw_key = f"hp_{int(owner)}"
            key = ((state.get("pvp_health_map") or {}).get(
                int(owner), raw_key if raw_key in state else key))
        return int((battle_state or {}).get(key, 0) or 0)
    if kind == "SourcePlayerThresholdAbilityVariable":
        state = battle_state or {}
        if variable.get("m_DontRecalculate", True):
            from .statistics import cached_ability_variable
            cached = cached_ability_variable(state, variable_name)
            if cached is not None:
                return int(cached)
        value = _source_player_threshold(state, owner, variable)
        if variable.get("m_DontRecalculate", True):
            from .statistics import cache_ability_variable
            value = cache_ability_variable(state, variable_name, value)
        return int(value)
    if kind == "IntAttrAbilityVariable":
        return _intattr_variable(
            db, session_id, battle_state, source_uid, owner,
            variable_name, variable)
    if kind in {"AbilityVariable", "CardIntegerVariable"}:
        if kind == "CardIntegerVariable":
            values = (battle_state or {}).get("card_integer_variables") or {}
            if variable_name in values:
                return int(values[variable_name] or 0)
            from pvp_db import db_card_mutation_field
            try:
                raw_buffs = db_card_mutation_field(
                    session_id, int(source_uid), "permanent_buffs", conn=db)
                saved = json.loads(raw_buffs or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                saved = {}
            values = saved.get("card_integer_variables", {}) \
                if isinstance(saved, dict) else {}
            return int(values.get(variable_name,
                                  variable.get("m_DefaultValue", 0)) or 0)
        values = (battle_state or {}).get("ability_variables") or {}
        return int(values.get(variable_name,
                              variable.get("m_DefaultValue", 0)) or 0)
    if kind == "SourcePlayerResourceAbilityVariable":
        side = _source_player_side(battle_state, owner)
        key = (f"{side}_resources" if variable.get(
            "m_LookUpTemporaryResources") else
            f"{side}_total_resources")
        if (battle_state or {}).get("pvp"):
            pid = int(owner or 0)
            raw_key = (f"res_{pid}" if variable.get("m_LookUpTemporaryResources")
                       else f"res_total_{pid}")
            if raw_key in (battle_state or {}):
                key = raw_key
        return int((battle_state or {}).get(
            key, variable.get("m_DefaultValue", 0)) or 0)
    if kind == "SourcePlayerShardAbilityVariable":
        return _source_player_shards(db, session_id, owner, variable)
    if kind == "CountListAttrAbilityVariable":
        return _count_list_variable(
            db, session_id, battle_state, source_uid, owner,
            variable, variable_name)
    if kind == "AbilityPropertyVariable":
        if str(variable.get("m_Property") or "") == "AbilityResourceXCost":
            return int((battle_state or {}).get("x_cost", 0) or 0)
        values = (battle_state or {}).get("ability_variables") or {}
        return int(values.get(variable_name, 0) or 0)
    if kind == "CardPropertyVariable":
        prop = str(variable.get("m_Property") or "")
        return _card_property_value(
            db, session_id, battle_state or {}, int(source_uid), prop)
    if kind in {"TriggerTargetPropertyVariable",
                "TriggerSourcePropertyVariable"}:
        state = battle_state or {}
        is_source = kind == "TriggerSourcePropertyVariable"
        trigger_uid = state.get("resolving_trigger_source_uid" if is_source
                                else "resolving_trigger_target_uid")
        event_data = state.get("resolving_trigger_event_data") or {}
        if trigger_uid is None:
            trigger_uid = event_data.get("source_card_id" if is_source
                                         else "target_card_id")
        if trigger_uid is None:
            return int(variable.get("m_DefaultValue", 0) or 0)
        prop = str(variable.get("m_Property") or "")
        return _card_property_value(
            db, session_id, state, int(trigger_uid), prop)
    if kind == "SourcePlayerBriarLegionVariable":
        state = battle_state or {}
        champion_uid = _champion_uid(state, owner)
        value = (_tac_stat(state, "cards", champion_uid,
                           "CardGameStats", "BriarLegionsPlayedThisGame")
                 if champion_uid is not None else None)
        return int(value if value is not None else
                   state.get("briar_legions_entered", 0) or 0)
    if kind == "SumVariableInListAttrCardsAbilityVariable":
        return _list_sum_variable(
            db, session_id, battle_state, raw, variable)
    if kind != "ExpressionAbilityVariable":
        return None
    default = int(variable.get("m_DefaultValue", 0) or 0)
    dont_recalculate = bool(variable.get("m_DontRecalculate", False))
    if dont_recalculate:
        from .statistics import cached_ability_variable
        cached = cached_ability_variable(battle_state, variable_name)
        if cached is not None:
            return int(cached)

    def finish(value):
        if value is None:
            value = default
        value = int(value)
        if dont_recalculate:
            from .statistics import cache_ability_variable
            value = cache_ability_variable(
                battle_state, variable_name, value)
        return int(value)

    try:
        tree = ast.parse(str(variable.get("m_ExpressionText") or ""),
                         mode="eval")
    except (SyntaxError, ValueError, TypeError):
        return finish(default)

    def visit(node):
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and isinstance(
                node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.Name):
            if node.id == "ESC":
                from .statistics import card_escalation_count
                return card_escalation_count(
                    db, session_id, battle_state, source_uid)
            if node.id == "INS":
                from .statistics import tac_stat
                value = tac_stat(
                    battle_state or {}, "cards", source_uid,
                    "CardStatsWithSpecificDuration", "InspireCount",
                    default=None)
                return int(value if value is not None else 0)
            resolved = _expression_value(
                db, session_id, battle_state, source_uid, owner, raw,
                node.id, stack)
            if resolved is not None:
                return resolved
            referenced = next((item for item in record.get("m_Variables", [])
                               if item.get("m_Name") == node.id), None)
            try:
                return int((referenced or {}).get("m_DefaultValue", 0) or 0)
            except (TypeError, ValueError):
                return 0
        if isinstance(node, ast.UnaryOp) and isinstance(
                node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp) and isinstance(
                node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            left, right = visit(node.left), visit(node.right)
            if left is None or right is None:
                raise ValueError("unknown expression variable")
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if right == 0:
                raise ValueError("division by zero")
            return left / right
        raise ValueError("unsupported expression syntax")

    try:
        value = visit(tree)
        if isinstance(value, bool):
            value = int(value)
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            return finish(default)
        return finish(value)
    except (ArithmeticError, TypeError, ValueError, OverflowError):
        return finish(default)


def _native_leaf_value(db, session_id, battle_state, source_uid, owner, param,
                       raw, *, allow_life_loss_modifier=False):
    if param.get("input_variable"):
        value = _count_variable(
            db, session_id, battle_state, source_uid, owner, raw,
            str(param["input_variable"]))
        if value is None:
            value = _sum_variable(
                db, session_id, battle_state, source_uid, owner, raw,
                str(param["input_variable"]))
        if value is None:
            value = _counter_variable(
                db, session_id, battle_state, source_uid, owner, raw,
                str(param["input_variable"]))
        if value is None:
            value = _expression_value(
                db, session_id, battle_state, source_uid, owner, raw,
                str(param["input_variable"]))
        if value is None:
            return None
        try:
            amount = int(param.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0
        if amount:
            value = amount * int(value)
        return str(param.get("property") or "").lower(), int(value)
    literal = _literal_leaf(
        param, extra_properties=("loselife",)
        if allow_life_loss_modifier else ())
    if literal is not None and literal[1] != 0:
        return literal
    # A zero CardModifier operand means “evaluate the authored variable” in
    # the client data.  Prefer typed metadata over a localized text guess.
    try:
        record = json.loads(raw or "{}")
        variables = record.get("m_Variables") or []
    except (TypeError, ValueError, json.JSONDecodeError):
        variables = []
    for variable in variables:
        name = variable.get("m_Name")
        if not name:
            continue
        value = _expression_value(
            db, session_id, battle_state, source_uid, owner, raw, name)
        if value is not None:
            prop = str(param.get("property") or "").lower()
            if (prop in {"attack", "defense", "cardcost", "intattr",
                         "damagemultiplier"} or
                    (allow_life_loss_modifier and prop == "loselife")):
                return prop, int(value)
    # Attribute grants commonly encode their operand in typed
    # ``attribute_flags`` while leaving CardModifier.amount at zero (for
    # example Emberleaf Duelist's Swiftstrike). Preserve the leaf so the
    # native static evaluator can project the keyword when its condition is
    # met.
    if str(param.get("property") or "").lower() == "attribute" and (
            param.get("attribute_flags") or param.get("attributeflags") or
            param.get("value")):
        return "attribute", 0
    return literal


def _target_matches(db, session_id, source_uid, source_owner, target_uid,
                    ability_guid, battle_state, cache=None):
    """Whether one continuous leaf's authored target set contains ``target``."""
    if cache is None:
        cache = _ProjectionCache()
    entries = _ability_target_entries(db, ability_guid, cache)
    if entries is None:
        # An ability with no authored target template modifies its own source.
        return int(source_uid) == int(target_uid)
    for template_id, self_only, both, _champions in entries:
        if self_only:
            if int(source_uid) == int(target_uid):
                return True
            continue
        if int(target_uid) in _template_target_set(
                db, session_id, source_owner, template_id, source_uid,
                both, battle_state, cache):
            return True
    return False


def _ability_target_entries(db, ability_guid, cache):
    """Return ``(template_id, self_only, both_players, champions)`` per target.

    The template list and its game-text classification are authored data, so
    they are resolved once per projection instead of once per projected card.
    ``champions`` records whether the template can select a champion in play,
    which the per-card projection cannot observe (champions have no
    ``game_cards`` row).  ``None`` means the ability authors no target
    template at all.
    """
    key = str(ability_guid or "").lower()
    cached = cache.ability_targets.get(key, _MISSING)
    if cached is not _MISSING:
        return cached
    from pvp_db import db_ability_target_template_ids, db_static_target_template
    from .targeting import template_targets_champions
    payload = db_ability_target_template_ids(ability_guid, conn=db)
    if not payload:
        cache.ability_targets[key] = None
        return None
    try:
        template_ids = json.loads(payload or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        template_ids = []
    entries = []
    for template_id in template_ids:
        row = db_static_target_template(template_id, conn=db)
        if not row:
            continue
        # db_static_target_template returns collection, player filter,
        # predicate, then game text. The self target belongs to the game text;
        # the player filter separately controls whether candidates may come
        # from both sides. Confusing these columns drops #SELF# auras and
        # changes the candidate side for ordinary target templates.
        text = str(row[3] or "").lower()
        player_filter = str(row[1] or "").lower()
        template = {"collection_flags": row[0] or "",
                    "player_filter": row[1] or "",
                    "filter_json": row[2] or "{}",
                    "game_text": row[3] or ""}
        entries.append((template_id,
                        "this" in text or "#self#" in text or
                        text.strip() == "you",
                        player_filter in {"multipleplayers", "allplayers",
                                          "multipleopponents"},
                        template_targets_champions(template)))
    entries = tuple(entries)
    cache.ability_targets[key] = entries
    return entries


def _template_target_set(db, session_id, controller_uid, template_id,
                         source_uid, both_players, battle_state, cache):
    """Return the candidate uids matching one authored target template.

    An aura only needs to know whether a single projected card is a legal
    target.  Materializing the candidate pool once per projection keeps that
    question linear in the board size instead of re-scanning (and re-projecting
    every candidate's threshold/subtype view) for every leaf and card.
    """
    state = battle_state if isinstance(battle_state, dict) else {}
    key = (int(session_id or 0), str(template_id).lower(),
           int(controller_uid or 0), int(source_uid or 0), bool(both_players),
           bool(state.get("_rules_port_suppress_card_properties")))
    cached = cache.target_sets.get(key)
    if cached is not None:
        return cached
    from .targeting import legal_targets
    try:
        candidates = legal_targets(
            db, session_id, int(controller_uid), template_id, int(source_uid),
            both_players=bool(both_players), battle_state=battle_state)
    except Exception:
        # A failing template resolves no targets, exactly as the per-leaf
        # probe did; do not memoize it so a transient failure can retry.
        return _EMPTY_TARGET_SET
    result = frozenset(int(value) for value in candidates)
    cache.target_sets[key] = result
    return result


class _StaticSession:
    def __init__(self, session_id):
        self.session_id = session_id


def _static_condition_matches(db, session_id, battle_state, param, source_uid,
                              source_owner, target_uid):
    condition_id = str(param.get("condition_id") or "")
    if not condition_id or condition_id == "0" * 36:
        return True
    from .condition_context import ConditionContext
    from .conditions import evaluate_effect_condition
    context = ConditionContext(
        db, _StaticSession(session_id), battle_state,
        event_type="AbilityEffectEvent",
        ability_source_uid=int(source_uid),
        ability_source_owner_id=int(source_owner),
        trigger_uid=int(target_uid))
    return bool(evaluate_effect_condition(db, condition_id, context))


def _native_static_deltas(db, session_id, battle_state, card_uid):
    """Evaluate the supported literal continuous Records leaves natively."""
    with _projection_cache() as cache:
        key = int(card_uid)
        if key in cache.scanning:
            # Re-entrant self-projection (a "for each troop" variable that
            # projects this same card): stop with no additional delta instead
            # of recursing forever.
            return _empty_deltas(), False
        cache.scanning.add(key)
        try:
            return _scan_static_deltas(
                db, session_id, battle_state, card_uid, cache)
        finally:
            cache.scanning.discard(key)


def _owner_static_sources(db, session_id, owner, cache):
    """Return the uids whose continuous abilities can project onto ``owner``."""
    from pvp_db import db_cards_in_zones_with_abilities
    key = (int(session_id or 0), int(owner or 0))
    cached = cache.source_cards.get(key)
    if cached is None:
        cached = tuple(int(uid) for uid, _abilities in
                       db_cards_in_zones_with_abilities(
                           session_id, int(owner),
                           ("warzone", "underground"), conn=db))
        cache.source_cards[key] = cached
    return cached


def _card_location_row(db, session_id, card_uid, cache):
    """Return ``(owner, location, position)`` for one card, memoized per scan."""
    from pvp_db import db_card_owner_location_position
    key = (int(session_id or 0), int(card_uid))
    cached = cache.card_rows.get(key, _MISSING)
    if cached is _MISSING:
        cached = db_card_owner_location_position(
            session_id, int(card_uid), conn=db)
        cache.card_rows[key] = cached
    return cached


def _owner_projects_card_properties(db, session_id, owner, cache):
    """Whether any source of ``owner`` can change a card's projected view.

    Only ``CardThreshold``/``SubType`` leaves feed the threshold/subtype
    projection.  When the owner has none — the common case — projecting a
    candidate's view is a no-op, so the pool scan skips an aura evaluation per
    candidate card.
    """
    key = (int(session_id or 0), int(owner or 0))
    cached = cache.card_property_sources.get(key)
    if cached is not None:
        return cached
    cached = any(
        str(param.get("property") or "").lower() in _CARD_PROPERTY_LEAVES
        for source_uid in _owner_static_sources(db, session_id, owner, cache)
        for ability_guid in _static_abilities(db, session_id, source_uid, cache)
        for param, _raw in _static_leaves(db, ability_guid, cache))
    cache.card_property_sources[key] = cached
    return cached


def is_continuous_self_static(graph):
    """Whether a CardCreated ability is a continuous self modifier.

    Such abilities ("This has cost -1 in all your zones for each Dwarf and/or
    Robot you control") are projected by this module in every zone, so they
    must not also resolve once at card creation.
    """
    from .metadata import modifier_metadata
    if graph is None or "CardCreatedEvent" not in str(
            graph.trigger_event_type or ""):
        return False
    zones = {value.lower() for value in
             str(graph.trigger_collection_flags or "").split("|") if value}
    if not {"deck", "hand", "warzone", "discard"} <= zones:
        return False
    if not graph.targets or not all(
            target.target_kind == "AbilitySourceCardTargetTemplate"
            for target in graph.targets):
        return False
    if not graph.effects:
        return False
    for effect in graph.effects:
        if (effect.concrete_type != "CardModifierAbilityEffectTemplate" or
                str(effect.duration).lower() != "permanent"):
            return False
        prop = str((modifier_metadata(effect.guid) or {}).get("property") or "")
        if prop not in {"attack", "defense", "cardcost", "attribute"}:
            return False
    return True


def _self_static_abilities(db, session_id, card_uid, location, cache):
    """A card's own continuous abilities that apply to itself where it is.

    Only in-play cards are scanned as aura sources, but "This has cost -1 in
    all your zones for each Dwarf and/or Robot you control" (Pterobot) must
    work in the hand.  Admit an ability of the card itself when it targets
    only its source and its collection flags include the card's zone.
    """
    from gamedata import DEFAULT_RECORD_STORE, ability_graph
    zone = str(location or "").lower()
    result = []
    for ability_guid in _static_abilities(db, session_id, card_uid, cache):
        graph = ability_graph(DEFAULT_RECORD_STORE, ability_guid)
        if graph is None or not graph.targets:
            continue
        if not all(target.target_kind == "AbilitySourceCardTargetTemplate"
                   for target in graph.targets):
            continue
        zones = {value.lower() for value in
                 str(graph.trigger_collection_flags or "").split("|") if value}
        if zone in zones:
            result.append(ability_guid)
    return result


def _scan_static_deltas(db, session_id, battle_state, card_uid, cache):
    row = _card_location_row(db, session_id, card_uid, cache)
    if not row:
        return _empty_deltas(), False
    owner, location, _position = row
    total = _empty_deltas()
    sources = [(source_uid, None) for source_uid in
               _owner_static_sources(db, session_id, owner, cache)]
    if int(card_uid) not in {uid for uid, _ in sources}:
        sources.append((int(card_uid), _self_static_abilities(
            db, session_id, card_uid, location, cache)))
    for source_uid, only_abilities in sources:
        abilities = (_static_abilities(db, session_id, source_uid, cache)
                     if only_abilities is None else only_abilities)
        for ability_guid in abilities:
            for param, raw in _static_leaves(db, ability_guid, cache):
                literal = _native_leaf_value(
                    db, session_id, battle_state, source_uid, owner, param, raw)
                property_name = str(param.get("property") or "").lower()
                # These modifiers change the target card's typed view rather
                # than its combat numbers.  Keep them in the same native
                # projection so a permanent aura cannot force the whole stat
                # calculation back through the historical static evaluator.
                if property_name in {"cardthreshold", "subtype"}:
                    if not _target_matches(
                            db, session_id, source_uid, int(owner),
                            int(card_uid), ability_guid, battle_state, cache):
                        continue
                    if not _static_condition_matches(
                            db, session_id, battle_state, param, source_uid,
                            int(owner), int(card_uid)):
                        continue
                    props = total["card_properties"].setdefault(
                        int(card_uid), {"thresholds": None,
                                        "subtype": None,
                                        "subtype_add": []})
                    if property_name == "cardthreshold":
                        shard = str(param.get("shard") or "").rsplit(
                            ".", 1)[-1].lower()
                        if shard in {"", "unknown", "invalid"}:
                            props["thresholds"] = []
                        else:
                            flag = int(game_engine.SHARD_TO_FLAG.get(shard, 0))
                            props["thresholds"] = [flag] if flag else []
                    else:
                        subtype = str(param.get("subtype") or "").strip()
                        operation = str(param.get("operation") or "set").lower()
                        if subtype:
                            if operation == "add":
                                props["subtype_add"].append(subtype)
                        else:
                            props["subtype"] = subtype
                    continue
                # Resource modifiers are activated effects, not card combat
                # deltas.  They can appear on a Permanent leaf in authored
                # data, but the resource lifecycle owns their application;
                # stat/cost projection must not delegate merely because it
                # encountered one while scanning a source ability.
                if property_name in {"currentresource", "totalresource",
                                      "chargepoints"}:
                    continue
                if property_name in {
                        "damagemultiplier", "damageimmunity", "attackimmunity",
                        "targetingimmunity", "blockimmunity",
                        "blockimmunityexception", "blockrestriction"}:
                    if not _target_matches(
                            db, session_id, source_uid, int(owner), int(card_uid),
                            ability_guid, battle_state, cache):
                        continue
                    if _static_condition_matches(
                            db, session_id, battle_state, param, source_uid,
                            int(owner), int(card_uid)):
                        rule = dict(param)
                        if property_name == "damagemultiplier" and literal is not None:
                            rule["value"] = literal[1]
                        total["rules"].append(rule)
                    continue
                if literal is None:
                    return total, True
                if not _target_matches(
                        db, session_id, source_uid, int(owner), int(card_uid),
                        ability_guid, battle_state, cache):
                    continue
                try:
                    if not _static_condition_matches(
                            db, session_id, battle_state, param, source_uid,
                            int(owner), int(card_uid)):
                        continue
                except RuntimeError:
                    return total, True
                prop, value = literal
                if prop == "attack":
                    total["atk"] += value
                elif prop == "defense":
                    total["def"] += value
                elif prop == "cardcost":
                    total["cost_mod"] += value
                elif prop == "attribute":
                    from .attribute_effects import attribute_bits_from_flags
                    total["attrs"] |= attribute_bits_from_flags(
                        param.get("attribute_flags",
                                  param.get("attributeflags",
                                           param.get("value", 0))))
                elif prop == "intattr":
                    attribute = str(param.get("attribute") or "").lower()
                    if attribute == "rage":
                        total["rage"] += value
                        total["attrs"] |= int(game_engine.ECardAttributes.Rage)
                    elif attribute in {"preventcombatdamage",
                                       "preventnoncombatdamage"}:
                        total["flags"].add(
                            "prevent_combat_damage" if attribute ==
                            "preventcombatdamage" else
                            "prevent_noncombat_damage")
                    elif attribute in {"preventmycombatdamage",
                                       "preventmynoncombatdamage"}:
                        total["flags"].add(
                            "prevent_my_combat_damage" if attribute ==
                            "preventmycombatdamage" else
                            "prevent_my_noncombat_damage")
                    elif attribute == "gladiator":
                        total["gladiator"] += value
                    elif attribute in {"cantgainhealth", "cantlosehealth",
                                       "cantplaycards", "unlimitedhandsize"}:
                        total["flags"].add(_CHAMPION_INTATTR_FLAGS[attribute])
                    elif attribute in {"lethal", "crush"}:
                        total["flags"].add(attribute)
                    else:
                        # IntAttr is also the client's storage for many
                        # ability-local markers (Tamed, Prophesied, and
                        # similar).  Those are not continuous combat stats;
                        # the old static layer ignores them, so do the same
                        # natively instead of delegating the whole projection.
                        continue
    return total, False


def rule_modifiers(db, session_id, battle_state, card_uid):
    """Return native typed rule modifiers affecting one card."""
    native, unsupported = _native_static_deltas(
        db, session_id, battle_state or {}, int(card_uid))
    from pvp_db import db_card_mutation_field
    result = list(native["rules"])
    for column in ("permanent_buffs", "temporary_buffs"):
        try:
            data = json.loads(db_card_mutation_field(
                session_id, int(card_uid), column, conn=db) or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            data = {}
        if isinstance(data, dict):
            result.extend(value for value in data.get("rule_modifiers", [])
                         if isinstance(value, dict))
    return result


def effective_card_properties(db, session_id, battle_state, card_uid):
    """Return the native current threshold/subtype view of a card.

    CardThresholdModifier and SubTypeModifier are not combat deltas, but
    target filters and casting requirements must observe them.  The guard
    prevents the static aura's own target query from recursively asking for
    the same projected view.
    """
    state = battle_state if isinstance(battle_state, dict) else {}
    marker = "_rules_port_suppress_card_properties"
    previous = state.get(marker)
    state[marker] = True
    try:
        props = {}
        with _projection_cache() as cache:
            row = _card_location_row(db, session_id, int(card_uid), cache)
            if row and _owner_projects_card_properties(
                    db, session_id, row[0], cache):
                native, _unsupported = _scan_static_deltas(
                    db, session_id, state, int(card_uid), cache)
                props = native.get("card_properties", {}).get(
                    int(card_uid), {}) or {}
    finally:
        if previous is None:
            state.pop(marker, None)
        else:
            state[marker] = previous
    from pvp_db import (db_card_mutation_field,
                        db_card_template_threshold_subtype)
    row = db_card_template_threshold_subtype(
        session_id, int(card_uid), conn=db)
    thresholds = []
    subtype = ""
    if row:
        from .targeting import _shards
        thresholds = list(_shards(row[0]))
        subtype = str(row[1] or "")
    try:
        buffs = json.loads(db_card_mutation_field(
            session_id, int(card_uid), "permanent_buffs", conn=db) or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        buffs = {}
    if isinstance(buffs, dict):
        if isinstance(buffs.get("thresholds"), list):
            thresholds = [int(value) for value in buffs["thresholds"]]
        if isinstance(buffs.get("subtype"), str):
            subtype = buffs["subtype"]
    if props.get("thresholds") is not None:
        thresholds = list(props["thresholds"])
    if props.get("subtype") is not None:
        subtype = str(props["subtype"])
    for value in props.get("subtype_add", ()):
        if value.lower() not in subtype.lower().split():
            subtype = (subtype + " " + value).strip()
    return thresholds, subtype


def _has_continuous_static(db, session_id, card_uid):
    from pvp_db import db_card_ability_payload, db_ability_static_metadata
    payload = db_card_ability_payload(session_id, int(card_uid), conn=db)
    try:
        abilities = json.loads(payload or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    for guid in abilities:
        metadata = db_ability_static_metadata(str(guid).lower(), conn=db)
        if not metadata:
            continue
        if not metadata[0] and not metadata[1]:
            return True
        # Only the authored all-zone CardCreatedEvent convention is
        # continuous.  Triggered abilities with broad collection flags (for
        # example Grave Nibbler) must not be treated as static modifiers.
        raw = str(metadata[2] or "")
        if ("CardCreatedEvent" in str(metadata[0] or "") and
                all(zone in raw for zone in ("Deck", "Hand", "Warzone", "Discard"))):
            return True
    return False


def _instance_buffs(row):
    attack = defense = rage = gladiator = 0
    attrs = int(row[3] or 0) | int(row[6] or 0) | int(row[9] or 0)
    flags = set()
    for raw in (row[7], row[8]):
        try:
            buffs = json.loads(raw or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            buffs = {}
        if not isinstance(buffs, dict):
            continue
        attack += int(buffs.get("atk", 0) or 0)
        defense += int(buffs.get("def", 0) or 0)
        rage += int(buffs.get("rage", 0) or 0)
        attrs |= int(buffs.get("attributes", 0) or 0)
        int_attrs = buffs.get("int_attrs", {})
        if isinstance(int_attrs, dict):
            rage += int(int_attrs.get("Rage", int_attrs.get("rage", 0)) or 0)
            if int(int_attrs.get("Lethal", int_attrs.get("lethal", 0)) or 0) > 0:
                flags.add("lethal")
            if int(int_attrs.get("Crush", int_attrs.get("crush", 0)) or 0) > 0:
                flags.add("crush")
            prevention = {
                "preventcombatdamage": "prevent_combat_damage",
                "preventnoncombatdamage": "prevent_noncombat_damage",
                "preventmycombatdamage": "prevent_my_combat_damage",
                "preventmynoncombatdamage": "prevent_my_noncombat_damage",
            }
            for name, value in int_attrs.items():
                lowered = str(name).lower()
                flag = prevention.get(lowered)
                if flag and int(value or 0) > 0:
                    flags.add(flag)
                elif lowered == "gladiator":
                    gladiator += int(value or 0)
        for rule in buffs.get("rule_modifiers", []) or []:
            if not isinstance(rule, dict) or rule.get("property") != "damagemultiplier":
                continue
            if int(rule.get("value", 0) or 0) <= 1:
                continue
            if rule.get("combatdamageonly"):
                flags.add("double_combat_damage")
            elif rule.get("noncombatdamageonly"):
                flags.add("double_noncombat_damage")
            else:
                flags.add("double_damage")
    return attack, defense, attrs, flags, rage, gladiator


def effective_stats(db, session_id, battle_state, card_uid):
    """Return current (attack, defense, attributes, flags, rage)."""
    static, unsupported = _native_static_deltas(
        db, session_id, battle_state or {}, int(card_uid))
    if unsupported:
        raise RuntimeError(
            f"RulesPort static stats have no native handler for card {card_uid}")
    return _stats_from_deltas(db, session_id, card_uid, static, battle_state)


def _is_active_owner(owner, battle_state):
    """Whether ``owner`` is the active player, or None when unknown."""
    if owner is None or not battle_state:
        return None
    if battle_state.get("pvp"):
        pid = battle_state.get("turn_pid")
        if pid is None:
            return None
        return int(owner) == int(pid)
    turn_player = battle_state.get("turn_player")
    if turn_player not in ("player", "ai"):
        return None
    return (turn_player == "player") == bool(int(owner or 0))


def _champion_int_attrs(battle_state, owner):
    """Runtime intattrs of the controller's synthetic champion card."""
    if owner is None or not battle_state:
        return {}
    champion_uid = None
    for participant, uid in (battle_state.get("champ_map") or {}).items():
        if str(participant) == str(owner):
            champion_uid = str(uid)
            break
    if champion_uid is None:
        return {}
    attrs = (battle_state.get("champion_int_attrs") or {}).get(champion_uid)
    return attrs if isinstance(attrs, dict) else {}


def _champion_has_gladiator_both(battle_state, owner):
    """Read GladiatorBoth from the controller's champion intattr state."""
    for name, value in _champion_int_attrs(battle_state, owner).items():
        if str(name).lower() == "gladiatorboth" and int(value or 0) > 0:
            return True
    return False


def _card_play_for_free(db, session_id, battle_state, card_uid):
    """Card.OwnerCanPlayForFree or the controller's CanPlayCardsForFree.

    ``PlayTroopTransaction.Resolve`` (and the spell/artifact equivalents)
    reject a free play unless one of these is set, so a client-projected play
    option must treat the card as zero cost while either holds.
    """
    if not isinstance(battle_state, dict) or not battle_state:
        return False
    from pvp_db import db_card_mutation_field, db_card_owner_id
    for column in ("permanent_buffs", "temporary_buffs"):
        try:
            data = json.loads(db_card_mutation_field(
                session_id, int(card_uid), column, conn=db) or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            data = {}
        attrs = data.get("int_attrs") if isinstance(data, dict) else None
        if isinstance(attrs, dict):
            for name, value in attrs.items():
                if (str(name).lower() == "ownercanplayforfree"
                        and int(value or 0) > 0):
                    return True
    owner = db_card_owner_id(session_id, int(card_uid), conn=db)
    for name, value in _champion_int_attrs(battle_state, owner).items():
        if (str(name).lower() == "canplaycardsforfree"
                and int(value or 0) > 0):
            return True
    return False


def _stats_from_deltas(db, session_id, card_uid, static, battle_state=None):
    """Project combat stats from an already-computed native delta view."""
    from pvp_db import db_card_static_row, db_card_combat_state
    try:
        row = db_card_static_row(session_id, int(card_uid), conn=db)
    except Exception:
        # Focused/older schemas predate the optional printed Rage/Lethal
        # columns.  The combat projection contains all baseline fields.
        row = db_card_combat_state(session_id, int(card_uid), conn=db)
        if row is not None:
            row = tuple(row) + (0, 0)
    if not row:
        return 0, 0, 0, set(), 0
    atk, defense, attrs, flags, rage, gladiator = _instance_buffs(row)
    atk += int(row[0] or 0) + int(row[4] or 0)
    defense += int(row[1] or 0) + int(row[5] or 0) - int(row[2] or 0)
    attrs |= int(row[3] or 0) | int(row[6] or 0)
    atk += int(static["atk"])
    defense += int(static["def"])
    attrs |= int(static["attrs"])
    flags |= set(static["flags"])
    rage += int(static["rage"])
    rage += int(row[10] or 0)
    if row[11]:
        flags.add("lethal")
    gladiator += int(static.get("gladiator", 0) or 0)
    if gladiator and battle_state is not None:
        from pvp_db import db_card_owner_id
        owner = db_card_owner_id(session_id, int(card_uid), conn=db)
        active = _is_active_owner(owner, battle_state)
        if active is not None:
            both = _champion_has_gladiator_both(battle_state, owner)
            # C# Card.CurrentAttackValue adds Gladiator for the active
            # controller and CurrentDefenseValue adds it while defending;
            # GladiatorBoth applies both halves at once.
            if active or both:
                atk += gladiator
            if not active or both:
                defense += gladiator
    return atk, max(0, defense), attrs, flags, rage


def effective_attributes(db, session_id, battle_state, card_uid):
    return effective_stats(db, session_id, battle_state, card_uid)[2]


def effective_deltas(db, session_id, battle_state, card_uid):
    native, unsupported = _native_static_deltas(
        db, session_id, battle_state or {}, int(card_uid))
    if unsupported:
        raise RuntimeError(
            f"RulesPort static deltas have no native handler for card {card_uid}")
    return native


def card_has_int_attr(db, session_id, card_uid, name):
    """Whether one card's runtime intattrs carry a positive named value."""
    from pvp_db import db_card_mutation_field
    wanted = str(name).lower()
    for column in ("permanent_buffs", "temporary_buffs"):
        try:
            data = json.loads(db_card_mutation_field(
                session_id, int(card_uid), column, conn=db) or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        attrs = data.get("int_attrs") if isinstance(data, dict) else None
        if isinstance(attrs, dict) and any(
                str(key).lower() == wanted and int(value or 0) > 0
                for key, value in attrs.items()):
            return True
    return False


def owner_has_card_int_attr(db, session_id, owner, name):
    """Whether any of ``owner``'s in-play cards carries the runtime intattr."""
    wanted = str(name).lower()
    rows = db.execute(
        "SELECT card_uid FROM game_cards WHERE session_id=? AND user_id=? "
        "AND location IN ('warzone', 'underground')",
        (session_id, int(owner))).fetchall()
    for (uid,) in rows:
        if card_has_int_attr(db, session_id, int(uid), wanted):
            return True
    return False


def champion_int_attribute(battle_state, owner, name):
    """One named runtime intattr from the controller's champion context."""
    value = champion_int_attr_optional(battle_state, owner, name)
    return 0 if value is None else value


def champion_int_attr_optional(battle_state, owner, name):
    """Like :func:`champion_int_attribute`, but None when the attr is absent."""
    for key, value in _champion_int_attrs(battle_state, owner).items():
        if str(key).lower() == str(name).lower():
            return int(value or 0)
    return None


def charge_point_cost_modifier(db, session_id, battle_state, owner,
                               source_uid):
    """Card.GetChargePointCostModifier for one ability activation.

    The modifier lives on the ability's source card; champion powers source
    from the champion, whose runtime intattrs carry modifiers granted by
    other cards.
    """
    from .combat_rules import card_int_attr
    total = 0
    if source_uid is not None:
        total += int(card_int_attr(
            db, session_id, int(source_uid),
            "ChargePointCostModifier") or 0)
    champion = None
    for participant, uid in (battle_state.get("champ_map") or {}).items():
        if str(participant) == str(owner):
            champion = int(uid)
            break
    if champion is not None and champion != int(source_uid or 0):
        total += champion_int_attribute(
            battle_state, owner, "ChargePointCostModifier")
    return total


def opposing_enters_play_exhausted(battle_state, controller_owner,
                                   template_guid):
    """Session.MoveCard: an opponent's champion flag taps entering troops.

    ``OpposingTroopsEnterPlayExhausted`` and
    ``OpposingNonArdentTroopsEnterPlayExhausted`` (non-Aria troops only) are
    read from every champion that opposes the entering troop's controller.
    """
    from gamedata import DEFAULT_RECORD_STORE
    card = DEFAULT_RECORD_STORE.get("CardTemplate", str(template_guid or "").lower())
    if card is None or "Troop" not in str(card.field("m_CardType", "") or ""):
        return False
    faction = str(card.field("m_Faction", "") or "").lower()
    for participant in (battle_state.get("champ_map") or {}):
        try:
            opponent = int(participant)
        except (TypeError, ValueError):
            continue
        if opponent == int(controller_owner or 0):
            continue
        for name, value in _champion_int_attrs(battle_state, opponent).items():
            if int(value or 0) <= 0:
                continue
            lowered = str(name).lower()
            if lowered == "opposingtroopsenterplayexhausted":
                return True
            if (lowered == "opposingnonardenttroopsenterplayexhausted"
                    and "aria" not in faction):
                return True
    return False


def mobilize_discount(db, session_id, owner_id, card_uid):
    """Session.CheckMobilize's affordable reduction for one card."""
    from pvp_db import db_warzone_blocker_uids
    from .combat_rules import card_int_attr
    limit = int(card_int_attr(db, session_id, int(card_uid), "Mobilize") or 0)
    if limit <= 0:
        return 0
    from domain.enums import ECardStates
    ready = db_warzone_blocker_uids(
        session_id, int(owner_id), int(ECardStates.Tapped), conn=db)
    return 2 * min(limit, len(ready))


def mobilize_payment(db, session_id, owner_id, card_uid, payment, mobilized):
    """Apply selected ``CardsToMobilize`` to a play payment.

    Returns the reduced payment, or ``None`` when the client's selection is
    not legal: more cards than the card's Mobilize value, or a card that is
    not one of the player's ready warzone troops.
    """
    if not mobilized:
        return max(0, int(payment or 0))
    from pvp_db import db_warzone_blocker_uids
    from .combat_rules import card_int_attr
    limit = int(card_int_attr(db, session_id, int(card_uid), "Mobilize") or 0)
    if limit <= 0 or len(mobilized) > limit:
        return None
    from domain.enums import ECardStates
    ready = {int(uid) for (uid,) in db_warzone_blocker_uids(
        session_id, int(owner_id), int(ECardStates.Tapped), conn=db)}
    if any(int(uid) not in ready for uid in mobilized):
        return None
    return max(0, int(payment or 0) - 2 * len(mobilized))


def effective_cost(db, session_id, battle_state, card_uid):
    native, unsupported = _native_static_deltas(
        db, session_id, battle_state or {}, int(card_uid))
    if unsupported:
        raise RuntimeError(
            f"RulesPort static cost has no native handler for card {card_uid}")
    if _card_play_for_free(db, session_id, battle_state, card_uid):
        return 0
    return _cost_from_deltas(db, session_id, card_uid, native)


def player_int_attributes(db, session_id, battle_state, owner_id, *,
                          target_uid=None, include_runtime=True):
    """Project active Records IntAttr modifiers onto a player or champion.

    Player permissions can come from active deck-top effects or continuous
    abilities on cards in play.  Keep the source zone, duration, target's
    player filter, and effect condition tied to Records metadata so player
    rules such as additional resource plays are available to both AI and
    transaction validation.  When ``target_uid`` is supplied, authored card
    target templates are also evaluated against that synthetic champion.  This
    lets damage resolution share the normal target metadata for effects such
    as ``OpposingChampions``.
    """
    from gamedata import DEFAULT_RECORD_STORE, ability_graph
    from pvp_db import (db_deck_top_card_details, db_card_ability_list,
                        db_cards_in_zones_with_abilities,
                        db_warzone_owner_ids,
                        db_get_champion_ability_guids)
    from .metadata import modifier_metadata
    owner = int(owner_id or 0)
    result = {}
    state = battle_state if isinstance(battle_state, dict) else {}

    # Synthetic champion instance IntAttrs are persisted in the shared
    # checkpoint because champion cards have no game_cards row.
    champion_map = state.get("champ_map") or {}
    if include_runtime:
        champion_uid = champion_map.get(owner, champion_map.get(str(owner)))
        try:
            champion_attrs = (state.get("champion_int_attrs") or {}).get(
                str(int(champion_uid)), {}) if champion_uid is not None else {}
        except (TypeError, ValueError):
            champion_attrs = {}
        if isinstance(champion_attrs, dict):
            for name, value in champion_attrs.items():
                try:
                    result[str(name)] = int(value or 0)
                except (TypeError, ValueError):
                    continue

    # Each source tuple records which authored duration can currently apply.
    # The top-of-deck source has its own lifetime; ordinary static player
    # modifiers require a source card to remain in the Warzone.  A champion's
    # own passive abilities are always available while that champion is in
    # the session.
    sources = []
    top = db_deck_top_card_details(session_id, owner, conn=db)
    if top:
        top_uid = int(top[1])
        sources.append((owner, top_uid, db_card_ability_list(
            session_id, top_uid, conn=db), {"WhileCardOnTopOfDeck"}))

    source_owners = {owner}
    for row in db_warzone_owner_ids(session_id, conn=db):
        try:
            source_owners.add(int(row[0]))
        except (TypeError, ValueError, IndexError):
            continue
    for key in champion_map:
        try:
            source_owners.add(int(key))
        except (TypeError, ValueError):
            continue
    champion_guids = state.get("champ_guid_map") or {}
    for source_owner in sorted(source_owners):
        for source_uid, abilities in db_cards_in_zones_with_abilities(
                session_id, source_owner, ("warzone",), conn=db):
            sources.append((source_owner, int(source_uid), abilities,
                            {"WhileCardInPlay"}))
        champion_guid = champion_guids.get(
            source_owner, champion_guids.get(str(source_owner)))
        source_champion_uid = champion_map.get(
            source_owner, champion_map.get(str(source_owner)))
        if champion_guid and source_champion_uid is not None:
            sources.append((source_owner, int(source_champion_uid),
                            db_get_champion_ability_guids(
                                champion_guid, conn=db),
                            {"WhileCardInPlay", "Permanent"}))

    def targets_player(player_filter, source_owner):
        target_filter = str(player_filter or "Self").rsplit(".", 1)[-1].lower()
        if target_filter in {"self", "you", "controller", "activeplayer"}:
            return int(source_owner) == owner
        if target_filter in {
                "opposing", "opponents", "multipleopponents"}:
            return int(source_owner) != owner
        if target_filter in {"multipleplayers", "allplayers"}:
            return True
        return False

    def targets_champion(target, source_owner, source_uid):
        """Match an authored target template against ``target_uid``."""
        if target_uid is None:
            return False
        from .filters import records_filter_matches
        from .targeting import (
            _player_filter_accepts,
            _source_card,
            target_template,
            template_targets_champions,
        )

        if getattr(target, "target_kind", "") == "PlayerTargetTemplate":
            return targets_player(target.player_filter, source_owner)

        player_filter = str(getattr(target, "player_filter", "") or "")
        if player_filter and not _player_filter_accepts(
                player_filter, owner, source_owner):
            return False

        template = target_template(db, str(getattr(target, "guid", "")))
        if not template or not template_targets_champions(template):
            return False
        filter_json = template.get("filter_json", "{}")
        if isinstance(filter_json, str):
            try:
                filter_json = json.loads(filter_json)
            except (TypeError, ValueError, json.JSONDecodeError):
                return False
        if not isinstance(filter_json, dict):
            return False

        source_card = _source_card(db, session_id, source_uid, source_owner)
        target_card = {
            "card_uid": int(target_uid),
            "card_type": "Champion",
            "location": "champions",
            "user_id": owner,
            "owner_id": owner,
            "controller_id": owner,
            "name": "Champion",
            "attack": 0,
            "defense": 0,
            "attributes": 0,
            "int_attrs": {},
        }
        return records_filter_matches(
            target_card,
            filter_json,
            source=source_card,
            context=dict(state),
            player=source_owner,
        )

    for source_owner, source_uid, abilities, active_durations in sources:
        if isinstance(abilities, str):
            try:
                abilities = json.loads(abilities or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                abilities = ()
        for ability_guid in abilities or ():
            graph = ability_graph(
                DEFAULT_RECORD_STORE, str(ability_guid).lower())
            if graph is None:
                continue
            for effect in graph.effects:
                if (effect.duration not in active_durations or
                        effect.concrete_type != "CardModifierAbilityEffectTemplate" or
                        effect.target_index < 0 or
                        effect.target_index >= len(graph.targets)):
                    continue
                target = graph.targets[effect.target_index]
                if target_uid is None:
                    if target.target_kind != "PlayerTargetTemplate":
                        continue
                    if not targets_player(target.player_filter, source_owner):
                        continue
                elif not targets_champion(target, source_owner, source_uid):
                    continue
                metadata = modifier_metadata(effect.guid)
                if str(metadata.get("property") or "").lower() != "intattr":
                    continue
                attribute = str(metadata.get("attribute") or "")
                if not attribute:
                    continue
                param = dict(metadata)
                param["condition_id"] = effect.condition_guid
                raw_ability = None
                try:
                    from pvp_db import db_ability_raw_json
                    raw_ability = db_ability_raw_json(
                        ability_guid, conn=db) or ""
                    resolved = _native_leaf_value(
                        db, session_id, state, source_uid, source_owner,
                        param, raw_ability)
                    if resolved is None or resolved[0] != "intattr":
                        continue
                    value = int(resolved[1])
                except (TypeError, ValueError, RuntimeError):
                    continue
                if effect.condition_guid and effect.condition_guid != "0" * 36:
                    try:
                        from .condition_context import ConditionContext
                        from .conditions import evaluate_effect_condition
                        if not evaluate_effect_condition(
                                db, effect.condition_guid, ConditionContext(
                                    db, _StaticSession(session_id), state,
                                    ability_source_uid=source_uid,
                                    ability_source_owner_id=source_owner,
                                    trigger_uid=source_uid)):
                            continue
                    except (TypeError, ValueError, RuntimeError):
                        continue
                operation = str(
                    metadata.get("operation") or "Set").lower()
                previous = int(result.get(attribute, 0) or 0)
                if metadata.get("double"):
                    result[attribute] = previous * 2
                elif operation in {"add", "increment"}:
                    result[attribute] = previous + value
                elif operation == "remove":
                    result.pop(attribute, None)
                elif operation == "subtract":
                    result[attribute] = previous - value
                else:
                    result[attribute] = value
    return result


def _cost_from_deltas(db, session_id, card_uid, native):
    """Project an effective cost from an already-computed native delta view."""
    from pvp_db import db_card_cost_location_state, db_card_mutation_field
    row = db_card_cost_location_state(session_id, int(card_uid), conn=db)
    if not row:
        return 0
    cost = int(row[0] or 0) + int(row[1] or 0)
    if row[2]:
        try:
            value = json.loads(row[2])
            cost += int(value.get("cost_mod", 0) or 0) if isinstance(value, dict) else 0
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    try:
        buffs = json.loads(db_card_mutation_field(
            session_id, int(card_uid), "temporary_buffs", conn=db) or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        buffs = {}
    if isinstance(buffs, dict):
        for modifier in buffs.get("temporary_cost_modifiers", ()) or ():
            if isinstance(modifier, dict):
                try:
                    cost += int(modifier.get("delta", 0) or 0)
                except (TypeError, ValueError):
                    continue
    return max(0, cost + int(native["cost_mod"]))


def effective_option_projection(db, session_id, battle_state, card_uid):
    """Return ``(attributes, cost)`` for one hand card from a single scan.

    The returned values are the same projections as :func:`effective_attributes`
    and :func:`effective_cost`; they share this evaluator rather than a parallel
    cost/attribute source.

    Play-option refreshes need both values for every card in hand.  Calling
    :func:`effective_attributes` and :func:`effective_cost` separately repeated
    the native static scan — including its target-filter evaluation — twice per
    card, which dominated the opponent-turn priority window.
    """
    native, unsupported = _native_static_deltas(
        db, session_id, battle_state or {}, int(card_uid))
    if unsupported:
        raise RuntimeError(
            "RulesPort static projection has no native handler for card "
            f"{card_uid}")
    attributes = _stats_from_deltas(
        db, session_id, card_uid, native, battle_state)[2]
    if _card_play_for_free(db, session_id, battle_state, card_uid):
        return attributes, 0
    return attributes, _cost_from_deltas(db, session_id, card_uid, native)


def _ability_targets_champions(db, ability_guid, cache):
    """Whether one continuous ability authors a champion target template."""
    entries = _ability_target_entries(db, ability_guid, cache)
    return bool(entries) and any(entry[3] for entry in entries)


def _champion_static_flags(db, session_id, battle_state, owner):
    """Rule flags this controller's champion-scoped statics impose.

    A continuous leaf only reaches a card through :func:`_scan_static_deltas`,
    which projects ``game_cards`` rows.  Champions are synthetic
    SessionCardIds with no such row, so a champion-targeted leaf never lands
    anywhere.  Fold those leaves into the controller's flags while their
    source is in play: Emberspire Witch's "Champions can't gain health" is a
    ``WhileCardInPlay`` ``CantGainHealth`` intattr on an AllChampions target.
    A champion's OWN passive (Construct Foreman's "Champions have no maximum
    hand size") has no card row at all, so its authored abilities are scanned
    from the checkpoint's ``champ_guid_map`` as well.
    """
    flags = set()
    owner = int(owner or 0)
    state = battle_state if isinstance(battle_state, dict) else {}
    champion_map = state.get("champ_map") or {}
    champion_uid = champion_map.get(owner, champion_map.get(str(owner)))
    champion_guid = (state.get("champ_guid_map") or {}).get(
        str(owner), (state.get("champ_guid_map") or {}).get(owner))

    def apply_leaves(source_uid, ability_guid, cache):
        if not _ability_targets_champions(db, ability_guid, cache):
            return
        for param, _raw in _static_leaves(db, ability_guid, cache):
            if str(param.get("property") or "").lower() != "intattr":
                continue
            flag = _CHAMPION_INTATTR_FLAGS.get(
                str(param.get("attribute") or "").lower())
            if not flag:
                continue
            if not _static_condition_matches(
                    db, session_id, state, param, source_uid,
                    owner, source_uid):
                continue
            flags.add(flag)

    with _projection_cache() as cache:
        for source_uid in _owner_static_sources(db, session_id, owner, cache):
            for ability_guid in _static_abilities(db, session_id, source_uid,
                                                  cache):
                apply_leaves(source_uid, ability_guid, cache)
        if champion_uid is not None and champion_guid:
            from pvp_db import db_get_champion_ability_guids
            for ability_guid in db_get_champion_ability_guids(
                    champion_guid, conn=db):
                apply_leaves(int(champion_uid), ability_guid, cache)
    return flags


def hand_size_unlimited(db, session_id, battle_state):
    """Whether any champion currently lifts the maximum hand size.

    The Construct Foreman passive is a ``WhileCardInPlay`` ``UnlimitedHandSize``
    intattr on an AllChampions target, so the rule covers both champions
    regardless of which side owns the source.  End-of-turn discard decisions
    ask this before applying the base hand limit.  Check every participant
    directly (not just warzone owners) because a champion passive applies even
    while its controller has no cards in play.
    """
    state = battle_state if isinstance(battle_state, dict) else {}
    owners = {0}
    for key in (state.get("champ_map") or {}):
        try:
            owners.add(int(key))
        except (TypeError, ValueError):
            continue
    for owner in owners:
        try:
            if "no_max_hand_size" in controller_flags(
                    db, session_id, state, owner):
                return True
        except (TypeError, ValueError, RuntimeError, AttributeError):
            continue
    return False


def controller_flags(db, session_id, battle_state, owner):
    """Return continuous combat/rule flags active for this controller.

    Per-card flags come from the controller's own warzone cards.  Flags that
    constrain champions are aggregated separately because champions have no
    card row for the per-card projection to land on.
    """
    from pvp_db import db_warzone_card_uids
    flags = _champion_static_flags(db, session_id, battle_state, owner)
    for (uid,) in db_warzone_card_uids(session_id, owner, conn=db):
        flags |= effective_stats(db, session_id, battle_state, int(uid))[3]
    return flags


def global_flags(db, session_id, battle_state):
    from pvp_db import db_warzone_owner_ids
    flags = set()
    for (owner,) in db_warzone_owner_ids(session_id, conn=db):
        flags |= controller_flags(db, session_id, battle_state, int(owner))
    return flags
