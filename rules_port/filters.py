"""Composable ports of the client ``CardFilter`` primitives.

Filters receive the same context shape as the C# engine but use duck-typed
runtime cards, keeping Records/metadata outside the rules kernel.
"""
from __future__ import annotations
from dataclasses import dataclass
from collections.abc import Iterable
import inspect
import json
import re
import threading
from typing import Any, Protocol, Sequence, cast

def _v(card: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(card, dict) and name in card: return card[name]
        if hasattr(card, name): return getattr(card, name)
    return default


def _attribute_bits(value):
    """Convert an ``ECardAttributes`` bitmask or '|'-joined enum names to bits.

    Records serializes attribute flags as a display string such as
    ``"Flight"`` or ``"Flight|SpellShield"``.  The client's
    ``HasAll/AnyAttributeFlags`` compare the decoded bitmask, so the adapter
    must decode the names rather than pass the string to ``int()``.
    """
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return int(value)
    import game_engine
    total = 0
    for name in str(value).split("|"):
        name = name.strip()
        if not name:
            continue
        total |= int(getattr(game_engine.ECardAttributes, name, 0) or 0)
    return total


def _shard_bits(value):
    """Convert an ``ECardShards`` bitmask or '|'-joined enum names to bits."""
    if value is None or value == "":
        return 0
    if isinstance(value, int):
        return int(value)
    import game_engine
    total = 0
    for name in str(value).split("|"):
        name = name.strip()
        if not name:
            continue
        total |= int(getattr(game_engine.ECardShards, name, 0) or 0)
    return total


def _shard_mask(value):
    if isinstance(value, (list, tuple, set)):
        result = 0
        for item in value:
            result |= _shard_bits(item)
        return result
    return _shard_bits(value)


def _card_type_value(card):
    raw = _v(card, "_card_type_name", "card_type_name", "card_type", "type",
             default=0)
    if isinstance(raw, str):
        from domain.enums import card_type_from_db
        return card_type_from_db(raw)
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def _champion_for(player_id, session):
    if player_id is None:
        return None
    for candidate in _candidate_cards(session):
        if str(_v(candidate, "location", "collection",
                   default="")).lower() not in {"champions", "champion"}:
            continue
        if _player_id(candidate) == player_id:
            return candidate
    return None


def _champion_class(champion):
    explicit = _v(champion, "champion_class", "class", default=None)
    if explicit is not None:
        return str(getattr(explicit, "name", explicit)).rsplit(".", 1)[-1]
    classes = _v(champion, "classes", "champion_classes", default=None)
    if classes:
        if isinstance(classes, str):
            return classes
        for value in classes:
            if value:
                return str(getattr(value, "name", value)).rsplit(".", 1)[-1]
    guid = str(_v(champion, "template_guid", "template_id",
                  default="") or "").lower()
    if not guid:
        return None
    try:
        from gamedata import DEFAULT_RECORD_STORE
    except (ImportError, AttributeError):
        return None
    for kind in ("ChampionTemplate", "CardTemplate"):
        record = DEFAULT_RECORD_STORE.get(kind, guid)
        if record is None:
            continue
        value = record.field("m_Class", record.field("m_ChampionClass"))
        if value is not None:
            return str(getattr(value, "name", value)).rsplit(".", 1)[-1]
    return None


def _faction_value(card):
    if card is None:
        return None
    attrs = _v(card, "int_attrs", "intattrs", default=None)
    if isinstance(attrs, dict):
        value = attrs.get("Faction", attrs.get("faction"))
        if value is not None:
            return value
    value = _v(card, "faction", "faction_flags", default=None)
    if value is not None:
        return value
    template = _template_of(card)
    if template is not None:
        return template.field("m_Faction")
    return None


def _subtype_tokens(card):
    raw = _v(card, "subtypes", "subtype", default=())
    if isinstance(raw, str):
        raw = raw.split(" ")
    tokens = set()
    for value in raw or ():
        for token in str(value).split(" "):
            token = token.strip().lower()
            if token and token != "of":
                tokens.add(token)
    return tokens


def _threshold_available(available, color):
    if not isinstance(available, dict):
        return 0
    from domain.enums import SHARD_TO_FLAG
    name = str(getattr(color, "name", color)).rsplit(".", 1)[-1]
    candidates = [color, name, name.lower()]
    try:
        candidates.append(int(color))
    except (TypeError, ValueError):
        pass
    flag = SHARD_TO_FLAG.get(name.lower())
    if flag is not None:
        candidates.append(flag)
    for key in candidates:
        if key in available:
            try:
                return int(available[key] or 0)
            except (TypeError, ValueError):
                return 0
    for key, value in available.items():
        if str(key).lower() == name.lower():
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0
    return 0


def _thresholds_of(card):
    thresholds = _v(card, "thresholds", "card_thresholds",
                    "resource_thresholds", default=None)
    if thresholds is None:
        template = _template_of(card)
        thresholds = (template.field("m_Threshold", ())
                      if template is not None else ())
    if isinstance(thresholds, dict):
        thresholds = thresholds.get("list", thresholds.get(
            "values", thresholds.get("thresholds", ())))
    return tuple(thresholds or ())


def _threshold_provided(card):
    provided = 0
    for threshold in _thresholds_of(card):
        if isinstance(threshold, dict):
            provided |= _shard_bits(threshold.get(
                "m_ColorFlags", threshold.get("color_flags",
                                              threshold.get("color"))))
        else:
            provided |= _shard_bits(threshold)
    return provided


def _card_shards(card):
    from domain.enums import ECardShards
    default = (ECardShards.AnyColor if _card_type_value(card) & 1
               else ECardShards.Colorless)
    shards = _v(card, "shards", "card_shards", default=None)
    if isinstance(shards, (list, tuple, set)):
        if not shards:
            return int(default)
        actual = 0
        for item in shards:
            actual |= _shard_bits(item)
        return actual
    actual = _shard_bits(_v(card, "color", "color_flags", "shard",
                            "shard_bits", default=0))
    return actual or int(default)


def _normalized_set_id(value):
    if isinstance(value, dict):
        value = value.get("m_Guid", value.get("guid", value.get("m_Id")))
    return str(value or "").lower() or None


def _card_set_id(card):
    if card is None:
        return None
    explicit = _v(card, "set_id", "card_set_id", default=None)
    if explicit is not None:
        return _normalized_set_id(explicit)
    template = _template_of(card)
    if template is None:
        return None
    return _normalized_set_id(template.field("m_SetId"))


def _card_set_number(card):
    explicit = _v(card, "set_number", default=None)
    if explicit is not None:
        try:
            return int(explicit)
        except (TypeError, ValueError):
            pass
    set_id = _card_set_id(card)
    if not set_id:
        return None
    try:
        from gamedata import DEFAULT_RECORD_STORE
        record = DEFAULT_RECORD_STORE.get("CardSetTemplate", set_id)
    except (ImportError, AttributeError, TypeError, ValueError):
        return None
    if record is None:
        return None
    value = record.field("m_SetNo", record.field("m_SetNumber"))
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _meets_thresholds(card, player):
    if card is None or player is None:
        return False
    available = _v(player, "resource_thresholds", "thresholds", default={}) or {}
    for requirement in _thresholds_of(card):
        if not isinstance(requirement, dict):
            return False
        color = requirement.get("color", requirement.get(
            "color_flags", requirement.get("m_ColorFlags")))
        quantity = requirement.get("quantity", requirement.get(
            "amount", requirement.get("m_Quantity", 0)))
        if _threshold_available(available, color) < int(quantity or 0):
            return False
    return True


def _casting_cost(card):
    if card is None:
        return 0
    explicit = _v(card, "casting_cost", default=None)
    if explicit is not None:
        try:
            return int(explicit or 0)
        except (TypeError, ValueError):
            return 0
    if _card_type_value(card) & 16:
        return 0
    try:
        base = int(_v(card, "resource_cost", "cost", default=0) or 0)
        adjustment = int(_v(card, "casting_cost_adjustment", default=0) or 0)
    except (TypeError, ValueError):
        return 0
    return base + adjustment


def _is_quick_speed(card):
    if card is None:
        return False
    try:
        attributes = int(_v(card, "attributes", "attribute_flags", default=0) or 0)
    except (TypeError, ValueError):
        attributes = 0
    if attributes & 268435456:
        return True
    attrs = _v(card, "int_attrs", "intattrs", default=None)
    if isinstance(attrs, dict):
        value = attrs.get("Quick", attrs.get("quick", 0))
        try:
            if int(value or 0):
                return True
        except (TypeError, ValueError):
            pass
    return bool(_v(card, "quick_action", "is_quick_action", default=False))


def _template_of(card):
    guid = str(_v(card, "template_guid", "template_id", default="") or "").lower()
    if not guid:
        return None
    try:
        from gamedata import DEFAULT_RECORD_STORE
        return DEFAULT_RECORD_STORE.get("CardTemplate", guid)
    except (ImportError, AttributeError, TypeError, ValueError):
        return None


def _template_flag(card, name, *, bit=0):
    flag = _v(card, name.lower(), default=None)
    if flag is not None:
        return bool(flag)
    template = _template_of(card)
    if template is not None:
        return bool(template.field(name, False))
    if bit:
        try:
            return bool(int(_v(card, "attributes", default=0) or 0) & bit)
        except (TypeError, ValueError):
            return False
    return False


def _has_subtype(card, value):
    raw = _v(card, "subtype", "subtypes", default="")
    if isinstance(raw, (list, tuple, set)):
        raw = " ".join(str(item) for item in raw)
    current = str(raw or "")
    if not current:
        return False
    if " " in str(value):
        return str(value) in current
    tokens = {token.lower() for token in current.split(" ")}
    return str(value).lower() in tokens


def _counter_value(card, counter_type):
    counters = _v(card, "counters", "card_counters", default={}) or {}
    if not isinstance(counters, dict) or counter_type in (None, ""):
        return 0
    wanted = str(counter_type).lower()
    if wanted == "invalid":
        return 0
    guids = _v(card, "counter_guids", default={}) or {}
    if not isinstance(guids, dict):
        guids = {}
    total = 0
    for name, value in counters.items():
        guid = str(guids.get(name, "")).lower()
        if str(name).lower() == wanted or (guid and guid == wanted):
            try:
                total += int(value or 0)
            except (TypeError, ValueError):
                continue
    return total


_GEM_SLOT_BITS = 10
_GEM_SLOT_MASK = (1 << _GEM_SLOT_BITS) - 1
_GEM_MINOR_TYPES = None
_GEM_MINOR_LOCK = threading.RLock()


def _gem_rows():
    try:
        import db as _db
        return tuple(_db._db.execute(
            "SELECT gem_type, gem_type_name, abilities_json FROM gem_templates"
        ).fetchall())
    except Exception:
        return ()


def _gem_minor_types():
    global _GEM_MINOR_TYPES
    with _GEM_MINOR_LOCK:
        if _GEM_MINOR_TYPES is None:
            _GEM_MINOR_TYPES = {
                int(row[0]) for row in _gem_rows()
                if "minor" in str(row[1]).lower()}
    return _GEM_MINOR_TYPES


def _socketed_gem_slots(card):
    try:
        gems = int(_v(card, "gems", "active_gem", "gem_flags", default=0) or 0)
    except (TypeError, ValueError):
        gems = 0
    for index in range(6):
        value = (gems >> (_GEM_SLOT_BITS * index)) & _GEM_SLOT_MASK
        if value:
            yield value


def _socketed_gem_count(card, *, must_be_minor=False):
    minor = _gem_minor_types() if must_be_minor else ()
    count = 0
    for value in _socketed_gem_slots(card):
        if must_be_minor and value not in minor:
            continue
        count += 1
    return count


def _gem_abilities_for_card(card):
    slots = tuple(_socketed_gem_slots(card))
    if not slots:
        return ()
    abilities = []
    rows = {int(row[0]): row[2] for row in _gem_rows()}
    for value in slots:
        try:
            values = json.loads(rows.get(value) or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            values = []
        for ability in values if isinstance(values, list) else ():
            if ability:
                abilities.append(str(ability).lower())
    return tuple(abilities)


def _printed_abilities_for_card(card):
    """Return the current instance's authored card abilities, if present."""
    values = _v(card, "card_abilities", "abilities", default=()) or ()
    if isinstance(values, str):
        try:
            values = json.loads(values)
        except (TypeError, ValueError, json.JSONDecodeError):
            values = ()
    if not isinstance(values, (list, tuple, set)):
        return ()
    return tuple(str(value).lower() for value in values if value)


def _tac_path_value(tree, parts):
    if not isinstance(tree, dict) or not parts:
        return None
    from .tac import _tac_attr_hash
    value = tree
    for name in parts:
        if not isinstance(value, dict):
            return None
        value = value.get(_tac_attr_hash(name))
    return value


def _responsibility_player(player, session, source):
    if isinstance(player, (int, str)) and not isinstance(player, bool):
        try:
            return int(player)
        except (TypeError, ValueError):
            return player
    resolved = _player_id(player)
    if resolved is not None:
        return resolved
    state = _runtime_state(session)
    for key in ("resolving_owner_id", "responsible_player_id", "active_player_id"):
        value = state.get(key)
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    return _player_id(source)


def _cmp(lhs, operation, rhs):
    op = str(getattr(operation, "name", operation)).replace("_", "").replace(" ", "").lower()
    return {"lessthan": lhs < rhs, "lessthanorequal": lhs <= rhs,
            "greaterthan": lhs > rhs, "greaterthanorequal": lhs >= rhs,
            "equals": lhs == rhs, "equal": lhs == rhs,
            "notequal": lhs != rhs, "notequals": lhs != rhs,
            "onemorethan": lhs == rhs + 1, "twomorethan": lhs == rhs + 2,
            "onelessthan": lhs == rhs - 1,
            "0": lhs < rhs, "1": lhs <= rhs, "2": lhs > rhs,
            "3": lhs >= rhs, "4": lhs == rhs, "5": lhs != rhs,
            "6": lhs == rhs + 1, "7": lhs == rhs + 2,
            "8": lhs == rhs - 1}.get(op, False)

def _candidate_cards(session: Any = None, cards: Any = None) -> tuple[Any, ...]:
    values = cards
    if values is None and session is not None:
        # Comparator leaves enumerate their own authored collection mask,
        # independent of the outer target template's candidate pool.
        values = _v(session, "all_cards", "cards", default=None)
        if values is None:
            iterator = getattr(session, "iter_cards", None)
            values = iterator() if callable(iterator) else None
    if isinstance(values, dict):
        values = values.values()
    return tuple(values) if isinstance(values, Iterable) else ()


def _runtime_state(session):
    if isinstance(session, dict):
        nested = session.get("bstate")
        return nested if isinstance(nested, dict) else session
    nested = getattr(session, "bstate", None)
    return nested if isinstance(nested, dict) else {}


def _card_by_uid(session, uid, *, source=None):
    try:
        wanted = int(uid)
    except (TypeError, ValueError):
        return None
    if source is not None and _id(source) == wanted:
        return source
    for candidate in _candidate_cards(session):
        try:
            if _id(candidate) == wanted:
                return (_records_filter_card(candidate)
                        if isinstance(candidate, dict) else candidate)
        except (TypeError, ValueError):
            continue
    return None


def _event_card(session, selector, *, source=None):
    state = _runtime_state(session)
    field = ("resolving_trigger_target_uid"
             if str(selector).lower() in {"triggertarget", "target"} else
             "resolving_trigger_source_uid")
    direct = state.get(field)
    if direct is None:
        event = state.get("resolving_trigger_event") or state.get(
            "trigger_event") or {}
        direct = _v(event, "target_card_id" if "target" in field else
                    "source_card_id", "target_uid" if "target" in field else
                    "source_uid", default=None)
    return _card_by_uid(session, direct, source=source)


def _stored_card_uids(session, source, *, list_name="StoredTargets"):
    state = _runtime_state(session)
    values = []
    ability_lists = state.get("ability_lists") or {}
    for name in (list_name, list_name[0].lower() + list_name[1:]):
        values.extend(ability_lists.get(name, ()) or ())
    ability_guid = str(state.get("resolving_ability") or "").lower()
    list_attrs = state.get("list_attrs") or {}
    scoped = list_attrs.get(ability_guid, {}) if isinstance(
        list_attrs, dict) else {}
    if isinstance(scoped, dict):
        values.extend(scoped.get(list_name, ()) or ())
    if source is not None:
        source_uid = _id(source)
        for scope in ("PermanentData", "ThisTurnsData"):
            try:
                from .statistics import tac_list
                values.extend(tac_list(state, "cards", source_uid, scope,
                                       list_name))
            except (ImportError, TypeError, ValueError):
                pass
        by_card = state.get("stored_targets_by_card") or {}
        values.extend(by_card.get(str(source_uid), by_card.get(source_uid, ()))
                      or ())
    result = []
    for value in values:
        if isinstance(value, dict):
            value = _v(value, "id", "Id", "card_uid", "session_card_id",
                       default=None)
        try:
            if value is not None:
                result.append(int(value))
        except (TypeError, ValueError):
            continue
    return tuple(result)


def _source_int_attrs(source):
    variables = _v(source, "card_integer_variables", default=None)
    if isinstance(variables, dict) and variables:
        return variables
    attrs = _v(source, "int_attrs", default={}) or {}
    if isinstance(attrs, str):
        try:
            import json
            attrs = json.loads(attrs)
        except (TypeError, ValueError):
            attrs = {}
    if not isinstance(attrs, dict):
        return {}
    nested = attrs.get("CardIntegerVariables") or attrs.get(
        "card_integer_variables")
    return nested if isinstance(nested, dict) else attrs


def _current_health(card):
    try:
        defense = int(_v(card, "defense", "defense_value", default=0) or 0)
        damage = int(_v(card, "damage", "damage_taken",
                        "current_damage", default=0) or 0)
    except (TypeError, ValueError):
        return 0
    return defense - damage


def _player_id(value):
    try:
        return int(_v(value, "player_id", "owner_id", "controller_id",
                       "user_id", "id",
                       default=value))
    except (TypeError, ValueError):
        return None


def _collection_bits(value):
    if value is None or value == "":
        return 0
    if isinstance(value, int):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    from domain.enums import ECardCollections
    flags = 0
    for name in str(value).replace(",", "|").split("|"):
        name = name.strip().rsplit(".", 1)[-1]
        if not name or name.lower() == "none":
            continue
        if name.lower() in {"crypt", "graveyard", "graveyards"}:
            name = "Discard"
        flags |= int(getattr(ECardCollections, name, 0) or 0)
    return flags


def _comparison_candidates(card, *, collection=None, player_filter=None,
                           card_filter=None, source=None, player=None,
                           cards=None,
                           session=None, effect=None, **kwargs):
    responsible = _player_id(player)
    flags = _collection_bits(collection)
    out = []
    for raw_candidate in _candidate_cards(session, cards):
        candidate = (_records_filter_card(raw_candidate)
                     if isinstance(raw_candidate, dict) else raw_candidate)
        if flags and not (int(_v(candidate, "collection", default=0) or 0)
                          & flags):
            continue
        # C# collection comparators iterate actual ECardCollections and a
        # None mask therefore visits no collection.
        if not flags:
            continue
        owner = _player_id(candidate)
        kind = str(player_filter or "").rsplit(".", 1)[-1].lower()
        if kind == "self" and owner != responsible:
            continue
        if kind in {"singleopponent", "multipleopponents"} and \
                owner == responsible:
            continue
        if card_filter is not None and not card_filter.matches(
                candidate, source=source, player=player, session=session,
                effect=effect, **kwargs):
            continue
        out.append(candidate)
    return tuple(out)

class CardFilter(Protocol):
    def matches(self, card, *, source=None, player=None, session=None, effect=None) -> bool: ...

@dataclass(frozen=True)
class InZone:
    collection: int
    def matches(self, card, **kwargs):
        try: return card is not None and bool(int(_v(card, "collection", "current_collection", default=0)) & int(self.collection))
        except (TypeError, ValueError): return False

@dataclass(frozen=True)
class IsTapped:
    value: bool = True
    def matches(self, card, **kwargs):
        tapped = _v(card, "is_tapped", "tapped", "is_exhausted", default=False)
        return bool(tapped() if callable(tapped) else tapped) is self.value

@dataclass(frozen=True)
class IsType:
    card_type: object
    def matches(self, card, **kwargs):
        wanted = self.card_type
        if isinstance(wanted, str):
            from domain.enums import card_type_from_db
            wanted = card_type_from_db(wanted)
        if not wanted:
            return False
        return bool(_card_type_value(card) & int(cast(Any, wanted)))

@dataclass(frozen=True)
class IsNotType(IsType):
    def matches(self, card, **kwargs):
        wanted = self.card_type
        if isinstance(wanted, str):
            from domain.enums import card_type_from_db
            wanted = card_type_from_db(wanted)
        if not wanted:
            return False
        return not IsType(wanted).matches(card, **kwargs)

@dataclass(frozen=True)
class InCollection:
    collection: int = 0
    def matches(self, card, **kwargs): return InZone(self.collection).matches(card)

@dataclass(frozen=True)
class HasAnyAttributeFlags:
    attribute_flags: int = 0
    def matches(self, card, **kwargs):
        try: return bool(int(_v(card, "attributes", "attribute_flags", default=0)) & _attribute_bits(self.attribute_flags))
        except (TypeError, ValueError): return False

@dataclass(frozen=True)
class HasAllAttributeFlags:
    attribute_flags: int = 0
    def matches(self, card, **kwargs):
        try:
            flags = _attribute_bits(self.attribute_flags)
            if not flags:
                return True
            return (int(_v(card, "attributes", "attribute_flags", default=0)) & flags) == flags
        except (TypeError, ValueError):
            return False

@dataclass(frozen=True)
class HasCastingCost:
    cost: int
    comparison: object = "Equals"
    def matches(self, card, **kwargs):
        try:
            return _cmp(_casting_cost(card), self.comparison, int(self.cost))
        except (TypeError, ValueError):
            return False

@dataclass(frozen=True)
class HasKeywordAbility:
    keyword: str
    def matches(self, card, **kwargs):
        if card is None:
            return False
        abilities = _v(card, "keywords", "abilities", default=()) or ()
        if self.keyword and self.keyword.lower() in {
                str(value).lower() for value in abilities}:
            return True
        for ability_guid in _v(card, "card_abilities", default=()) or ():
            template = None
            try:
                from gamedata import DEFAULT_RECORD_STORE
                template = DEFAULT_RECORD_STORE.get(
                    "AbilityTemplate", str(ability_guid).lower())
            except (ImportError, AttributeError, TypeError, ValueError):
                template = None
            if template is None:
                continue
            from .tac import decode_tac_tree, _tac_attr_hash
            serialized = template.field("m_SerializedTAC") or {}
            data = serialized.get("data", "") if isinstance(serialized, dict) \
                else serialized
            tree = decode_tac_tree(data)
            if isinstance(tree, dict) and bool(
                    tree.get(_tac_attr_hash(self.keyword))):
                return True
        return False

@dataclass(frozen=True)
class HasTag:
    tag: str
    def matches(self, card, **kwargs):
        if not self.tag:
            return False
        tags = _v(card, "tags", "tag", default=())
        if isinstance(tags, str):
            tags = (tags,)
        return self.tag.lower() in {str(value).lower() for value in (tags or ())}

@dataclass(frozen=True)
class IsSubType:
    subtype: str
    def matches(self, card, **kwargs):
        if not self.subtype:
            return False
        return self.subtype == "*" or _has_subtype(card, self.subtype)

class IsMultiThresholdCard:
    def matches(self, card, **kwargs):
        values = _v(card, "thresholds", "resource_thresholds", default=())
        return len(values or ()) > 1

def _resource_cost_rhs(self, source, player, session, effect, **kwargs):
    rhs = int(self.cost or 0)
    state = _runtime_state(session)
    if self.resource_cost_card_filter is not None:
        rhs = 0
        for candidate in _candidate_cards(session):
            if str(_v(candidate, "location", default="")).lower() != \
                    "warzone":
                continue
            if self.resource_cost_card_filter.matches(
                    _records_filter_card(candidate)
                    if isinstance(candidate, dict) else candidate,
                    source=source, player=player, session=session,
                    effect=effect, **kwargs):
                rhs += 1
    if self.add_x:
        ability_x = int(_v(
            effect, "x_cost", "resource_x_cost",
            default=state.get("ability_x_cost", state.get("x_cost", 0)))
                        or 0)
        rhs += (ability_x if ability_x > 0 else int(_v(
            source, "resource_x_cost_paid", "card_x_cost_paid", default=0)
                or 0))
    if self.add_sum_list_attr_name:
        name = str(self.add_sum_list_attr_name)
        values = (state.get("ability_lists") or {}).get(name, ()) or ()
        for value in values:
            uid = (_v(value, "id", "Id", "card_uid", default=value)
                   if isinstance(value, dict) else value)
            list_card = _card_by_uid(session, uid, source=source)
            if str(self.add_sum_property).rsplit(".", 1)[-1].lower() \
                    in {"", "unknown"}:
                rhs += 1
            elif list_card is not None:
                prop = str(self.add_sum_property).rsplit(".", 1)[-1]
                aliases = {"CurrentAttackValue": ("attack", "attack_value"),
                           "CurrentHealthValue": ("defense", "defense_value"),
                           "ResourceCost": ("resource_cost", "cost"),
                           "CastingCost": ("casting_cost", "cost"),
                           "CurrentDefenseValue": ("defense", "defense_value")}
                rhs += int(_v(list_card, *aliases.get(prop, (prop,)),
                              default=0) or 0)
    if self.add_attack:
        rhs += int(_v(source, "attack", "attack_value", default=0) or 0)
    if self.add_defense:
        rhs += _current_health(source)
    if self.add_card_integer_variable and source is not None:
        rhs += int(_source_int_attrs(source).get(
            self.add_card_integer_variable, 0) or 0)
    if self.add_variable:
        variable_name = (self.add_variable.get("m_InputVariableName", "")
                         if isinstance(self.add_variable, dict) else
                         str(self.add_variable))
        variables = (_v(effect, "variables", "ability_variables",
                        default=None) or state.get("ability_variables") or
                     state.get("variables") or {})
        rhs += int(variables.get(variable_name, 0) or 0)
    return rhs


@dataclass(frozen=True)
class HasResourceCost:
    cost: int = 0
    comparison: object = 0
    add_x: bool = False
    add_attack: bool = False
    add_defense: bool = False
    add_card_integer_variable: str = ""
    add_variable: str = ""
    add_sum_list_attr_name: str = ""
    add_sum_property: object = "Unknown"
    resource_cost_card_filter: CardFilter | None = None
    def matches(self, card, *, source=None, player=None, session=None, effect=None,
                **kwargs):
        if card is None:
            return False
        lhs = int(_v(card, "resource_cost", "cost", default=0) or 0)
        lhs += int(_v(card, "resource_x_cost_paid", "x_cost", default=0) or 0)
        rhs = _resource_cost_rhs(self, source, player, session, effect,
                                 **kwargs)
        return _cmp(lhs, self.comparison, rhs)

@dataclass(frozen=True)
class HasAttackValue:
    value: int = 0
    comparison: object = 0
    compare_to_source: bool = False
    compare_to_source_controlled_card_filter_count: CardFilter | None = None
    compare_to_card_integer_variable: str = ""
    compare_to_stored_target: bool = False
    compare_to_trigger_source: bool = False
    def matches(self, card, *, source=None, player=None, session=None,
                **kwargs):
        try:
            rhs = int(self.value)
            if self.compare_to_source and source is not None:
                rhs = int(_v(source, "attack", "attack_value", default=0) or 0)
            elif self.compare_to_source_controlled_card_filter_count is not None:
                responsible = _player_id(player)
                rhs = sum(1 for candidate in _candidate_cards(session)
                          if str(_v(candidate, "location", default="")).lower()
                          == "warzone" and _player_id(candidate) == responsible
                          and self.compare_to_source_controlled_card_filter_count.matches(
                              _records_filter_card(candidate)
                              if isinstance(candidate, dict) else candidate,
                              source=source, player=player, session=session,
                              **kwargs))
            elif (self.compare_to_card_integer_variable and
                  self.compare_to_card_integer_variable in
                  _source_int_attrs(source)):
                rhs += int(_source_int_attrs(source).get(
                    self.compare_to_card_integer_variable, 0) or 0)
            elif self.compare_to_stored_target:
                stored = _stored_card_uids(session, source)
                other = _card_by_uid(session, stored[0], source=source) \
                    if stored else None
                if other is None:
                    return False
                rhs = int(_v(other, "attack", "attack_value", default=0) or 0)
            elif self.compare_to_trigger_source:
                other = _event_card(session, "TriggerSource", source=source)
                if other is None:
                    return False
                rhs = int(_v(other, "attack", "attack_value", default=0) or 0)
            return _cmp(int(_v(card, "attack", "attack_value", default=-1)), self.comparison, rhs)
        except (TypeError, ValueError): return False

@dataclass(frozen=True)
class HasDefenseValue:
    value: int = 0
    comparison: object = 0
    compare_to_source: bool = False
    def matches(self, card, *, source=None, **kwargs):
        try:
            rhs = (int(_v(source, "defense", "defense_value", default=0) or 0)
                   if self.compare_to_source and source is not None else
                   int(self.value))
            return _cmp(int(_v(card, "defense", "defense_value", default=-1)),
                        self.comparison, rhs)
        except (TypeError, ValueError):
            return False

@dataclass(frozen=True)
class IsSocketable:
    socket_value: int
    comparison: object = "Equals"
    def matches(self, card, **kwargs):
        try: return _cmp(int(_v(card, "socket_count", default=0)), self.comparison, int(self.socket_value))
        except (TypeError, ValueError): return False

@dataclass(frozen=True)
class IsSocketed:
    socketed_value: int = 0
    comparison: object = "Equals"
    must_be_minor: bool = False
    compare_to_ability_source: bool = False
    def matches(self, card, *, source=None, **kwargs):
        if card is None:
            return False
        if self.compare_to_ability_source:
            try:
                gems = int(_v(card, "gems", "active_gem", "gem_flags", default=0) or 0)
                other = int(_v(source, "gems", "active_gem", "gem_flags", default=0) or 0)
            except (TypeError, ValueError):
                return False
            return bool(gems & other)
        legacy = _v(card, "socketed_count", "active_gem_count", default=None)
        if legacy is not None or _v(card, "gems", "active_gem", "gem_flags",
                                     default=None) is None:
            if legacy is None:
                legacy = 1 if _v(card, "is_socketed", "socketed", default=False) else 0
            value = int(legacy or 0)
        else:
            value = _socketed_gem_count(card, must_be_minor=self.must_be_minor)
        return _cmp(int(value), self.comparison, int(self.socketed_value))

@dataclass(frozen=True)
class HasName:
    name: str
    use_stored_name: bool = False
    def matches(self, card, *, source=None, session=None, **kwargs):
        needle = self.name
        if self.use_stored_name:
            state = _runtime_state(session)
            stored_names = state.get("stored_names") or {}
            ability_guid = str(state.get("resolving_ability") or "").lower()
            names = stored_names.get(ability_guid)
            if not names and source is not None:
                names = stored_names.get(str(_id(source)))
            needle = (names[-1] if names else "")
            if source is not None and not needle:
                # ``state["cards"]`` is a dict of per-card attr maps in battle
                # state, but a target-evaluation context stores the candidate
                # list under the same key; normalize before indexing.
                cards = state.get("cards")
                cards = cards if isinstance(cards, dict) else {}
                for scope in ("PermanentData", "ThisTurnsData"):
                    data = (cards.get(str(_id(source))) or
                            cards.get(_id(source)) or {})
                    attrs = data.get(scope) if isinstance(data, dict) else None
                    if isinstance(attrs, dict) and attrs.get("StoredName"):
                        needle = attrs["StoredName"]
                        break
            return str(_v(card, "name", "template_name", default="")) == \
                str(needle or "")
        if needle == "<this>" and source is not None:
            needle = _v(source, "name", "template_name", default="")
        value = _v(card, "name", "template_name", default="")
        return str(needle).lower() in str(value).lower()

@dataclass(frozen=True)
class IsRarity:
    rarity: object
    def matches(self, card, **kwargs): return _v(card, "rarity", "card_rarity") == self.rarity

@dataclass(frozen=True)
class IsColor:
    color: object
    include_resources: bool = False
    prismatic: bool = False
    def matches(self, card, **kwargs):
        if card is None:
            return False
        if self.prismatic:
            return len(_thresholds_of(card)) > 1
        wanted = _shard_mask(self.color)
        if not wanted:
            return False
        if _card_shards(card) & wanted:
            return True
        if self.include_resources and _card_type_value(card) & 16:
            return bool(_threshold_provided(card) & wanted)
        return False

@dataclass(frozen=True)
class InFaction:
    faction: object
    def matches(self, card, **kwargs):
        value = None
        attrs = _v(card, "int_attrs", "intattrs", default=None)
        if isinstance(attrs, dict):
            value = attrs.get("Faction", attrs.get("faction"))
        if value is None:
            value = _v(card, "faction", "faction_flags", default=None)
        if value is None:
            template_guid = str(_v(
                card, "template_guid", default="") or "").lower()
            if template_guid:
                try:
                    from gamedata import DEFAULT_RECORD_STORE
                    template = DEFAULT_RECORD_STORE.get(
                        "CardTemplate", template_guid)
                    value = (template.field("m_Faction")
                             if template is not None else None)
                except (ImportError, AttributeError, TypeError, ValueError):
                    value = None
        # C# EFactions is a plain enum, compared with equality (not flags).
        try:
            return (int(cast(Any, value)) ==
                    int(cast(Any, self.faction)))
        except (TypeError, ValueError):
            return value == self.faction

class IsToken:
    def matches(self, card, **kwargs):
        if card is None:
            return False
        return bool(_card_type_value(card) & 4096) or bool(
            _v(card, "is_token", "token", default=False))

@dataclass(frozen=True)
class IsResource:
    threshold_color_flags: object = "Unknown"
    is_basic_resource: bool = True
    is_non_standard_resource: bool = False
    def matches(self, card, **kwargs):
        if card is None:
            return False
        if not (_card_type_value(card) & 16 or bool(
                _v(card, "is_resource", default=False))):
            return False
        basic = bool(_v(card, "is_basic_resource", default=
                        "standard" in str(_v(
                            card, "subtype", "subtypes", default="")).lower()))
        if self.is_basic_resource and not basic:
            return False
        if self.is_non_standard_resource and basic:
            return False
        flag = str(self.threshold_color_flags or "Unknown").rsplit(".", 1)[-1]
        if flag.lower() == "any":
            raw_type = _v(card, "_card_type_name", "card_type_name", default=None)
            if raw_type is not None:
                return str(raw_type).lower() == "resource"
            return _card_type_value(card) == 16
        wanted = _shard_mask(self.threshold_color_flags)
        return (_threshold_provided(card) & wanted) == wanted

class IsQuick:
    def matches(self, card, **kwargs):
        return card is not None and _is_quick_speed(card)

class IsTroop(IsType):
    def __init__(self): super().__init__(2)

class IsArtifact(IsType):
    def __init__(self): super().__init__(32)

class IsChampion(IsType):
    def __init__(self): super().__init__(1)

class IsHero:
    must_be_ai: bool = False
    must_be_human: bool = False
    def matches(self, card, **kwargs):
        if card is None:
            return False
        return bool(_card_type_value(card) & 1) or bool(
            _v(card, "is_hero", "hero", default=False))

@dataclass(frozen=True)
class IsAlternateArt:
    alternate_art_pref: object = 0
    def matches(self, card, **kwargs):
        pref = str(getattr(self.alternate_art_pref, "name",
                            self.alternate_art_pref)).rsplit(".", 1)[-1].lower()
        flag = _template_flag(card, "m_HasAlternateArt")
        if flag is False and card is not None:
            flag = bool(_v(card, "alternate_art", "is_alternate_art",
                           default=False))
        if pref in ("any", "0", ""):
            return True
        if pref in ("onlyaa", "2"):
            return flag
        return not flag

@dataclass(frozen=True)
class IsExtendedArt:
    extended_art_only: bool = False
    def matches(self, card, **kwargs):
        if not self.extended_art_only:
            return True
        return bool(_v(card, "is_extended", "extended_art", "is_extended_art",
                       default=False))

@dataclass(frozen=True)
class IsPromo:
    has_alternate_art: int = -1
    def matches(self, card, **kwargs):
        if self.has_alternate_art < 0:
            return True
        flag = _template_flag(card, "m_HasAlternateArt")
        if flag is False and card is not None:
            flag = bool(_v(card, "promo", "is_promo", default=False))
        return (flag and self.has_alternate_art == 1) or (
            not flag and self.has_alternate_art == 0)

class IsAttacking:
    def matches(self, card, **kwargs): return bool(_v(card, "is_attacking", "attacking", default=False))

class IsBlocking:
    def matches(self, card, **kwargs): return bool(_v(card, "is_blocking", "blocking", default=False))

@dataclass(frozen=True)
class IsControlledBy:
    test_against_active_player: bool = False
    def matches(self, card, *, player=None, session=None, source=None, **kwargs):
        if card is None:
            return False
        controller = _v(card, "controller_id", "controlling_player", "owner_id")
        if self.test_against_active_player:
            expected = getattr(session, "active_player_id", None)
            if expected is None:
                expected = _runtime_state(session).get("active_player_id")
            return controller == expected
        expected = _responsibility_player(player, session, source)
        return source is not None and controller is not None and \
            controller == expected

@dataclass(frozen=True)
class IsNotControlledBy:
    fail_uncontrolled_cards: bool = False
    def matches(self, card, *, player=None, session=None, source=None, **kwargs):
        if card is None:
            return False
        controller = _v(card, "controller_id", "controlling_player", "owner_id")
        expected = _responsibility_player(player, session, source)
        return (not self.fail_uncontrolled_cards or controller is not None) and \
            controller != expected

class IsPlayedThisTurn:
    def matches(self, card, **kwargs): return bool(_v(card, "played_this_turn", "came_out_this_turn", default=False))

class IsDamagedThisTurn:
    def matches(self, card, **kwargs): return bool(_v(card, "damaged_this_turn", "damaged", default=False))

class IsHealedThisTurn:
    def matches(self, card, **kwargs): return bool(_v(card, "healed_this_turn", "healed", default=False))

class AnyCard:
    def matches(self, card, **kwargs): return True

class IsBasic:
    def matches(self, card, **kwargs):
        return card is not None and not _is_quick_speed(card)

@dataclass(frozen=True)
class IsUniqueCard:
    unique: int = -1
    def matches(self, card, **kwargs):
        if self.unique < 0:
            return True
        flag = _template_flag(card, "m_Unique", bit=256)
        return (flag and self.unique == 1) or (not flag and self.unique == 0)

@dataclass(frozen=True)
class IsCardName:
    name: str = ""
    compare_to_ability_source: bool = False
    compare_to_trigger_source: bool = False
    compare_to_trigger_target: bool = False
    def matches(self, card, *, source=None, session=None, **kwargs):
        wanted = self.name
        if self.compare_to_trigger_source:
            event_card = _event_card(session, "TriggerSource", source=source)
            if event_card is None:
                return False
            wanted = _v(event_card, "name", "template_name", default="")
        elif self.compare_to_trigger_target:
            event_card = _event_card(session, "TriggerTarget", source=source)
            if event_card is None:
                return False
            wanted = _v(event_card, "name", "template_name", default="")
        if self.compare_to_ability_source:
            wanted = _v(source, "name", "template_name", default="")
        return str(_v(card, "name", "template_name", default="")).lower() == \
            str(wanted or "").lower()

@dataclass(frozen=True)
class NameContainsFilter:
    value: str
    include_subtype: bool = False
    include_keywords: bool = False
    language_code: str = ""
    def matches(self, card, **kwargs):
        pattern = str(self.value or "")
        if not pattern:
            return True
        if pattern == "changed":
            pattern = "1.1.0.086"
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except (re.error, TypeError):
            return False
        name = str(_v(card, "name", "template_name", default="") or "")
        if regex.search(name):
            return True
        if self.include_subtype:
            card_type = str(_v(card, "_card_type_name", "card_type_name",
                               "card_type", "type", default="") or "")
            subtype = str(_v(card, "subtype", "subtypes", default="") or "")
            if regex.search((card_type + " " + subtype).strip()):
                return True
        if self.include_keywords:
            game_text = str(_v(card, "ability_game_text", "keywords_text",
                               default="") or "")
            if not game_text:
                try:
                    from gamedata import DEFAULT_RECORD_STORE
                    template = DEFAULT_RECORD_STORE.get(
                        "CardTemplate", str(_v(
                            card, "template_guid", "template_id", default="")
                            or "").lower())
                    for guid in getattr(template, "ability_guids", ()):
                        ability = DEFAULT_RECORD_STORE.get(
                            "AbilityTemplate", str(guid).lower())
                        if ability is not None:
                            game_text += " " + str(getattr(
                                ability, "game_text", "") or "")
                except (ImportError, AttributeError, TypeError, ValueError):
                    pass
            if regex.search(game_text):
                return True
        return False

class HasAttackedThisTurn:
    def matches(self, card, **kwargs): return bool(_v(card, "attacked_this_turn", "has_attacked_this_turn", default=False))

@dataclass(frozen=True)
class CompareAttackAndDefenseFilter:
    comparison: object = 0
    compare_to_ability_source_defense: bool = False
    compare_to_ability_source_attack: bool = False
    def matches(self, card, *, source=None, **kwargs):
        attack = int(_v(card, "attack", "attack_value", default=0) or 0)
        defense = int(_v(card, "defense", "defense_value", default=0) or 0)
        if self.compare_to_ability_source_defense:
            rhs = int(_v(source, "defense", "defense_value", default=0) or 0)
            return _cmp(attack, self.comparison, rhs)
        if self.compare_to_ability_source_attack:
            rhs = int(_v(source, "attack", "attack_value", default=0) or 0)
            return _cmp(defense, self.comparison, rhs)
        return _cmp(attack, self.comparison, defense)

@dataclass(frozen=True)
class CompareAttackToLowestFilter:
    comparison: object = 0
    card_filter: CardFilter | None = None
    collection: object = None
    player_filter: object = None
    def matches(self, card, *, session=None, source=None, cards=None,
                player=None, effect=None, **kwargs):
        if self.card_filter is not None and not self.card_filter.matches(
                card, source=source, player=player, session=session,
                effect=effect, **kwargs):
            return False
        candidates = _comparison_candidates(
            card, collection=self.collection, player_filter=self.player_filter,
            card_filter=self.card_filter, source=source, player=player,
            cards=cards, session=session, effect=effect, **kwargs)
        values = [int(_v(c, "attack", "attack_value", default=0) or 0)
                  for c in candidates]
        extreme = min(values) if values else 2147483647
        return _cmp(int(_v(card, "attack", "attack_value", default=0) or 0),
                    self.comparison, extreme)

@dataclass(frozen=True)
class CompareAttackToHighestFilter(CompareAttackToLowestFilter):
    def matches(self, card, *, session=None, source=None, cards=None,
                player=None, effect=None, **kwargs):
        if self.card_filter is not None and not self.card_filter.matches(
                card, source=source, player=player, session=session,
                effect=effect, **kwargs):
            return False
        candidates = _comparison_candidates(
            card, collection=self.collection, player_filter=self.player_filter,
            card_filter=self.card_filter, source=source, player=player,
            cards=cards, session=session, effect=effect, **kwargs)
        values = [int(_v(c, "attack", "attack_value", default=0) or 0)
                  for c in candidates]
        extreme = max(values) if values else -2147483648
        return _cmp(int(_v(card, "attack", "attack_value", default=0) or 0),
                    self.comparison, extreme)

@dataclass(frozen=True)
class CompareDefenseToLowestFilter:
    comparison: object = 0
    def matches(self, card, *, session=None, cards=None, player=None,
                **kwargs):
        responsible = _player_id(player)
        candidates = []
        for candidate in _candidate_cards(session, cards):
            if str(_v(candidate, "location", default="")).lower() != "warzone":
                continue
            if responsible is not None and _player_id(candidate) != responsible:
                continue
            candidates.append(candidate)
        # The shipped C# class named Lowest calls GetMaxValueFromCollection.
        values = [int(_v(c, "defense", "defense_value", default=0) or 0)
                  for c in candidates]
        return _cmp(
            int(_v(card, "defense", "defense_value", default=0) or 0),
            self.comparison, max(values) if values else 0)

@dataclass(frozen=True)
class CompareHealthToLowestFilter:
    comparison: object = 0
    def matches(self, card, *, session=None, cards=None, **kwargs):
        if not IsChampion().matches(card):
            return False
        values = [int(_v(c, "health", "current_health", "defense", default=0) or 0)
                  for c in _candidate_cards(session, cards)
                  if IsChampion().matches(c)]
        return _cmp(
            int(_v(card, "health", "current_health", "defense", default=0) or 0),
            self.comparison, min(values) if values else 0)

@dataclass(frozen=True)
class CompareHealthToHighestFilter(CompareHealthToLowestFilter):
    def matches(self, card, *, session=None, cards=None, **kwargs):
        if not IsChampion().matches(card):
            return False
        values = [int(_v(c, "health", "current_health", "defense", default=0) or 0)
                  for c in _candidate_cards(session, cards)
                  if IsChampion().matches(c)]
        return _cmp(
            int(_v(card, "health", "current_health", "defense", default=0) or 0),
            self.comparison, max(values) if values else 0)

@dataclass(frozen=True)
class CompareResourceCostToHighestFilter:
    comparison: object = 0
    card_filter: CardFilter | None = None
    collection: object = None
    player_filter: object = None
    def matches(self, card, *, session=None, source=None, cards=None,
                player=None, effect=None, **kwargs):
        if self.card_filter is not None and not self.card_filter.matches(
                card, source=source, player=player, session=session,
                effect=effect, **kwargs):
            return False
        candidates = _comparison_candidates(
            card, collection=self.collection, player_filter=self.player_filter,
            card_filter=self.card_filter, source=source, player=player,
            cards=cards, session=session, effect=effect, **kwargs)
        values = [int(_v(c, "resource_cost", "cost", default=0) or 0)
                  for c in candidates]
        return _cmp(
            int(_v(card, "resource_cost", "cost", default=0) or 0),
            self.comparison, max(values) if values else -2147483648)

@dataclass(frozen=True)
class CompareResourceCostToMyHighestFilter(CompareResourceCostToHighestFilter):
    def matches(self, card, *, session=None, cards=None, player=None,
                source=None, **kwargs):
        responsible = _responsibility_player(player, session, source)
        values = [int(_v(candidate, "resource_cost", "cost", default=0) or 0)
                  for candidate in _candidate_cards(session, cards)
                  if str(_v(candidate, "location", default="")).lower() ==
                  "warzone" and (responsible is None or
                  _player_id(candidate) == responsible)]
        return _cmp(int(_v(card, "resource_cost", "cost", default=0) or 0),
                    self.comparison, max(values) if values else 0)

@dataclass(frozen=True)
class HasSourceTypeFilter:
    dont_exactly_match_original: bool = False
    def matches(self, card, *, source=None, **kwargs):
        if card is None or source is None:
            return False
        if self.dont_exactly_match_original:
            a = _v(card, "template_guid", "template_id", default=None)
            b = _v(source, "template_guid", "template_id", default=None)
            if a is not None and a == b:
                return False
        return _v(card, "card_type", "type", default=0) == _v(source, "card_type", "type", default=0)

class DifferentOwners:
    def matches(self, card, *, source=None, player=None, session=None, **kwargs):
        if card is None:
            return False
        controller = _v(card, "controller_id", "owner_id", default=None)
        expected = _responsibility_player(player, session, source)
        return controller != expected

@dataclass(frozen=True)
class HasASharedFactionWithSourceFilter:
    def matches(self, card, *, source=None, **kwargs):
        return _faction_value(card) == _faction_value(source)

@dataclass(frozen=True)
class HasASharedRarityWithSourceFilter:
    def matches(self, card, *, source=None, **kwargs):
        return _v(card, "rarity", "card_rarity", default=None) == _v(source, "rarity", "card_rarity", default=None)

@dataclass(frozen=True)
class HasASharedSubtypeWithSourceFilter:
    def matches(self, card, *, source=None, **kwargs):
        return bool(_subtype_tokens(card) & _subtype_tokens(source))

class HasASharedClassWithSourceChampionFilter:
    def matches(self, card, *, source=None, player=None, session=None,
                **kwargs):
        champion = _champion_for(
            _responsibility_player(player, session, source), session)
        if champion is None:
            champion = source
        if champion is None or card is None:
            return False
        champion_class = _champion_class(champion)
        if not champion_class:
            return False
        return _has_subtype(card, champion_class)

class HasASharedSubtypeWithSourceChampionFilter(HasASharedSubtypeWithSourceFilter):
    def matches(self, card, *, source=None, player=None, session=None,
                **kwargs):
        champion = _champion_for(
            _responsibility_player(player, session, source), session)
        if champion is None:
            champion = source
        return card is not None and champion is not None and bool(
            _subtype_tokens(card) & _subtype_tokens(champion))

@dataclass(frozen=True)
class HasASharedShardWithSourceFilter:
    exact_match: bool = False
    stored_shard: bool = False

    def matches(self, card, *, source=None, session=None, **kwargs):
        # The ordinary shared-shard branch also excludes Colorless. Keep the
        # enum import outside the StoredShard arm so both branches match C#.
        import game_engine
        def bits(value):
            # Records projections carry shards either as a decoded flag list
            # (targeting._card) or as a colour bitmask/enum-name string.  C#
            # compares the flag bits, so accept both shapes.
            if isinstance(value, (list, tuple, set)):
                total = 0
                for item in value:
                    total |= _shard_bits(item)
                return total
            return _shard_bits(value)
        try:
            actual = bits(_v(card, "shards", "shard", "color_flags", default=0))
            if self.stored_shard:
                permanent = _v(source, "permanent_data", "PermanentData",
                               default={}) or {}
                chosen = _v(permanent, "ChosenShards", "chosen_shards",
                            default=None)
                if chosen is None and source is not None:
                    try:
                        from .statistics import tac_list
                        chosen = tac_list(session, "cards", _id(source),
                                          "PermanentData", "ChosenShards")
                    except (ImportError, TypeError, ValueError):
                        chosen = ()
                wanted = 0
                for shard in chosen or ():
                    if isinstance(shard, dict):
                        for name in ("Blood", "Ruby", "Sapphire", "Wild",
                                     "Diamond"):
                            if bool(shard.get(name, shard.get(name.lower(), 0))):
                                wanted |= int(getattr(
                                    game_engine.ECardShards, name, 0) or 0)
                    else:
                        wanted |= bits(shard)
            else:
                wanted = bits(_v(source, "shards", "shard", "color_flags",
                                 default=0))
        except (TypeError, ValueError):
            return False
        if self.exact_match:
            return actual == wanted
        return bool(actual & wanted & ~int(game_engine.ECardShards.Colorless))

class HasASharedShardWithTopOfChainFilter(HasASharedShardWithSourceFilter):
    def matches(self, card, *, session=None, source=None, **kwargs):
        top = _v(session, "top_of_chain", "chain_top",
                 "top_of_chain_card", default=None)
        if top is None and source is not None:
            top = source
        return super().matches(card, source=top, session=session, **kwargs)

class MovedBySource:
    def matches(self, card, *, source=None, **kwargs):
        if card is None or source is None:
            return False
        moved = _v(card, "moved_by_source_id", "last_moved_by", default=None)
        if moved is None:
            permanent = _v(card, "permanent_data", "PermanentData", default={})
            if isinstance(permanent, dict):
                moved = permanent.get(
                    "IdOfCardThatMovedMeLast",
                    permanent.get("id_of_card_that_moved_me_last"))
        try:
            return (int(cast(Any, moved) or 0) != 0 and
                    int(cast(Any, moved)) == _id(source))
        except (TypeError, ValueError):
            return False

class IsChildOfAbilitySource:
    def matches(self, card, *, source=None, **kwargs):
        if card is None or source is None:
            return False
        parent = _v(card, "parent_link", "parent_id", "parent_uid",
                    default=None)
        try:
            parent = int(parent or 0)
        except (TypeError, ValueError):
            return False
        return parent != 0 and parent == _id(source)

class IsParentOfAbilitySourceFilter:
    def matches(self, card, *, source=None, **kwargs):
        if source is None or card is None:
            return False
        parent = _v(source, "parent_link", "parent_id", "parent_uid",
                    default=None)
        try:
            parent = int(parent or 0)
        except (TypeError, ValueError):
            return False
        return parent != 0 and parent == _id(card)

class InCombatWithSourceFilter:
    def matches(self, card, *, source=None, session=None, **kwargs):
        if card is None or source is None:
            return False
        card_id = _id(card)
        source_id = _id(source)
        for attacker, blockers in _combat_blocker_map(session).items():
            if attacker == card_id and source_id in blockers:
                return True
            if attacker == source_id and card_id in blockers:
                return True
        combat = getattr(card, "in_combat_with", None)
        if callable(combat):
            return bool(combat(source, session))
        return False

@dataclass(frozen=True)
class DamagedOpponentThisTurn:
    only_combat_damage: bool = False
    only_non_combat_damage: bool = False
    def matches(self, card, *, session=None, **kwargs):
        if card is None:
            return False
        if self.only_combat_damage:
            name = "CombatDamageDealtToOpponent"
            legacy = "combat_damage_dealt_to_opponent"
        elif self.only_non_combat_damage:
            name = "NonCombatDamageDealtToOpponent"
            legacy = "non_combat_damage_dealt_to_opponent"
        else:
            name = "DamageDealtToOpponent"
            legacy = "damage_dealt_to_opponent"
        stats = _v(card, "stats_this_turn", "card_stats_this_turn",
                   default=None)
        if isinstance(stats, dict):
            value = stats.get(name, stats.get(name.lower(), stats.get(legacy, 0)))
            return int(value or 0) > 0
        uid = _v(card, "card_uid", "session_card_id", "id", default=None)
        try:
            from .statistics import tac_stat
            value = tac_stat(_runtime_state(session), "cards", uid,
                             "CardStatsThisTurn", name, default=None)
        except (ImportError, TypeError, ValueError):
            value = None
        if value is not None:
            return int(value or 0) > 0
        flagged = _v(card, "damaged_opponent_this_turn", legacy, default=None)
        if isinstance(flagged, (list, tuple, set)):
            return bool(flagged)
        return int(flagged or 0) > 0

@dataclass(frozen=True)
class HasSourceResourceCost:
    comparison: object = "Equals"
    def matches(self, card, *, source=None, **kwargs):
        if card is None or source is None:
            return False
        return _cmp(int(_v(card, "resource_cost", "cost", default=0) or 0), self.comparison,
                    int(_v(source, "resource_cost", "cost", default=0) or 0))

@dataclass(frozen=True)
class HasSourceCastingCostFilter:
    comparison: object = 0
    add_value: int = 0
    use_source: bool = False
    def matches(self, card, *, source=None, effect=None, context=None,
                **kwargs):
        if card is None or source is None:
            return False
        selected_source = source
        if self.use_source and effect is not None:
            selected_source = _v(
                effect, "source_card", "ability_source_card", "true_source",
                default=source) or source
        runtime_context = context if context is not None else effect
        state = (runtime_context if isinstance(runtime_context, dict) else
                 getattr(runtime_context, "bstate", None))
        if not isinstance(state, dict):
            state = {}
        variables = {}
        if (runtime_context is not None and
                not isinstance(runtime_context, dict)):
            from .fields import ability_variables
            variables.update(ability_variables(
                getattr(runtime_context, "ability", None)))
        variables.update(state.get("ability_variables") or {})
        try:
            from .fields import resolve_field
            bonus = resolve_field(self.add_value, variables=variables,
                                  outputs=state.get("effect_outputs") or state,
                                  battle_state=state)
        except (ImportError, TypeError, ValueError):
            try:
                bonus = int(self.add_value or 0)
            except (TypeError, ValueError):
                bonus = 0
        lhs = _casting_cost(card)
        lhs += int(_v(card, "resource_x_cost_paid", "x_cost",
                      default=0) or 0)
        rhs = _casting_cost(selected_source)
        rhs += int(_v(selected_source, "resource_x_cost_paid", "x_cost",
                      default=0) or 0) + int(bonus or 0)
        return _cmp(lhs, self.comparison, rhs)

@dataclass(frozen=True)
class CompareCastingCostToSourceCountersFilter:
    comparison: object = "Equals"
    counter_type: object = None
    def matches(self, card, *, source=None, **kwargs):
        if card is None or source is None:
            return False
        count = _counter_value(source, self.counter_type)
        cost = int(_v(card, "resource_cost", "cost", default=0) or 0)
        cost += int(_v(card, "resource_x_cost_paid", "x_cost",
                        default=0) or 0)
        return _cmp(cost, self.comparison, int(count or 0))

@dataclass(frozen=True)
class HasCountersValue:
    amount: int = 0
    comparison: object = 0
    counter_type: object = None
    def matches(self, card, **kwargs):
        if card is None:
            return False
        value = _counter_value(card, self.counter_type)
        return _cmp(int(value or 0), self.comparison, int(self.amount))

def _attr_path(card, path, default=None):
    value = card
    for part in str(path or "").split(">"):
        if part.lower() == "card":
            continue
        value = _v(value, part, part.lower(), default=default)
        if value is default:
            break
    return value

@dataclass(frozen=True)
class IntAttrFilter:
    attribute: str = ""
    comparison: object = 0
    value: int = 0
    compare_to_cost: bool = False
    def matches(self, card, *, source=None, player=None, session=None,
                effect=None, **kwargs):
        if card is None:
            return False
        attr = str(self.attribute or "")
        parts = [part for part in attr.split(">") if part]
        prefix = parts[0].lower() if parts else ""
        target = card
        if prefix == "card":
            parts = parts[1:]
        elif prefix.startswith("you"):
            target = _champion_for(
                _responsibility_player(player, session, source), session)
            if target is None:
                target = source
            parts = parts[1:]
        elif prefix == "abilitytac":
            target = effect
            parts = parts[1:]
            # RulesPort effect conditions receive the originating TAC as
            # ``ConditionContext.event_tac``.  Preserve AbilityTAC filters
            # when they arrive through a wrapped Records CardFilter (for
            # example RequiresSourcePassesFilterCondition ->
            # IntAttrFilter(AbilityTAC>PlayedFromHand)).  Without this, the
            # filter falls through to the ConditionContext object, which has
            # no serialized IntAttrs, and incorrectly evaluates every event
            # attribute as zero.
            event_tac = getattr(effect, "event_tac", None)
            if event_tac is None:
                event_state = getattr(effect, "bstate", {}) or {}
                event_tac = event_state.get("event_tac")
            if isinstance(event_tac, dict) and parts:
                from .tac import _tac_attr_hash
                attribute_hash = _tac_attr_hash(parts[0])
                value = event_tac.get(attribute_hash)
                if value is None:
                    value = event_tac.get(str(attribute_hash))
                if value is not None:
                    try:
                        rhs = (int(_v(card, "resource_cost", "cost", default=0)
                                    or 0) if self.compare_to_cost else
                               int(self.value))
                        return _cmp(int(value or 0), self.comparison, rhs)
                    except (TypeError, ValueError):
                        return False
        if any(part.lower() == "rabid" for part in parts):
            return self._matches_rabid(card, parts)
        path = ">".join(parts) if parts else attr
        lhs: Any = None
        values = _v(target, "int_attrs", "intattrs", default=None)
        if isinstance(values, dict) and values:
            lhs = values.get(path, values.get(path.lower(), values.get(
                path.capitalize())))
        if lhs is None:
            tree = _v(target, "tac_tree", "serialized_tac_tree",
                      default=None)
            if isinstance(tree, dict):
                lhs = _tac_path_value(tree, parts)
        if lhs is None:
            lhs = _attr_path(target, path, 0)
        try:
            rhs = (int(_v(card, "resource_cost", "cost", default=0) or 0)
                   if self.compare_to_cost else int(self.value))
            if _cmp(int(lhs or 0), self.comparison, rhs):
                return True
        except (TypeError, ValueError):
            return False
        if self.compare_to_cost:
            return False
        from .tac import decode_tac_tree
        ability_guids = list(_printed_abilities_for_card(card))
        ability_guids.extend(_gem_abilities_for_card(card))
        for ability_guid in dict.fromkeys(ability_guids):
            template = None
            try:
                from gamedata import DEFAULT_RECORD_STORE
                template = DEFAULT_RECORD_STORE.get(
                    "AbilityTemplate", str(ability_guid).lower())
            except (ImportError, AttributeError, TypeError, ValueError):
                template = None
            if template is None:
                continue
            serialized = template.field("m_SerializedTAC") or {}
            data = (serialized.get("data", "")
                    if isinstance(serialized, dict) else serialized)
            tree = decode_tac_tree(data)
            value = _tac_path_value(tree, parts) if isinstance(tree, dict) \
                else None
            if value is None:
                continue
            try:
                if _cmp(int(cast(Any, value) or 0), self.comparison,
                        int(self.value)):
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def _matches_rabid(self, card, parts):
        names = [part for part in parts if part.lower() != "rabid"]
        from .tac import decode_tac_tree, _tac_attr_hash
        for ability_guid in _v(card, "card_abilities", default=()) or ():
            template = None
            try:
                from gamedata import DEFAULT_RECORD_STORE
                template = DEFAULT_RECORD_STORE.get(
                    "AbilityTemplate", str(ability_guid).lower())
            except (ImportError, AttributeError, TypeError, ValueError):
                template = None
            if template is None:
                continue
            serialized = template.field("m_SerializedTAC") or {}
            data = serialized.get("data", "") if isinstance(serialized, dict) \
                else serialized
            tree = decode_tac_tree(data)
            if not isinstance(tree, dict):
                continue
            value: Any = tree
            for name in names:
                if not isinstance(value, dict):
                    value = None
                    break
                value = value.get(_tac_attr_hash(name))
            if value is not None and _cmp(int(value or 0), self.comparison,
                                          int(self.value)):
                return True
        return False

@dataclass(frozen=True)
class StringAttrFilter:
    attribute: str = ""
    comparison: object = "Equals"
    value: str = ""
    def matches(self, card, **kwargs):
        lhs = str(_attr_path(card, self.attribute, "") or "")
        rhs = str(self.value)
        op = str(getattr(self.comparison, "name", self.comparison)).lower().replace(" ", "")
        return {"equals": lhs == rhs, "equal": lhs == rhs,
                "notequal": lhs != rhs, "notequals": lhs != rhs,
                "lessthan": lhs < rhs, "lessthanorequal": lhs <= rhs,
                "greaterthan": lhs > rhs, "greaterthanorequal": lhs >= rhs,
                "0": lhs < rhs, "1": lhs <= rhs, "2": lhs > rhs, "3": lhs >= rhs,
                "4": lhs == rhs, "5": lhs != rhs}.get(op, lhs == rhs)

@dataclass(frozen=True)
class SetIdFilter:
    set_id: object = None
    def matches(self, card, **kwargs):
        return _card_set_id(card) == _normalized_set_id(self.set_id)

@dataclass(frozen=True)
class SetNumberFilter:
    set_number: int = -1
    def matches(self, card, **kwargs):
        if self.set_number <= 0:
            return True
        if card is None:
            return False
        if bool(_v(card, "is_basic_resource", default=
                   "standard" in str(_v(card, "subtype", default="")).lower())):
            return True
        return _card_set_number(card) == self.set_number

@dataclass(frozen=True)
class MatchesTargetFilter:
    target_index: int = 0
    match_name: bool = True
    def matches(self, card, *, effect=None, **kwargs):
        targets = _v(effect, "target_map", "targets", default={}) or {}
        selected = targets.get(self.target_index, targets.get(str(self.target_index), ())) if isinstance(targets, dict) else ()
        if isinstance(selected, dict): selected = selected.get("cards", selected.get("session_card_ids", ()))
        name = _v(card, "name", "template_name", default=None)
        return any((name == _v(item, "name", "template_name", default=item)) for item in (selected or ()))

@dataclass(frozen=True)
class TopNOfDeck:
    amount: int = 1
    filter: CardFilter | None = None
    add_sapphire: bool = False
    add_x: bool = False
    add_removed_counters: bool = False
    add_removed_counters_multiplier: int = 1
    add_source_cards_attack: bool = False
    add_source_cards_defense: bool = False
    add_source_cards_cost: bool = False
    add_card_integer_variable: str = ""
    add_damage_dealt: bool = False
    add_damage_that_would_be_dealt: bool = False
    top_half_of_deck: bool = False
    count_from_bottom: bool = False
    def matches(self, card, *, session=None, cards=None, source=None,
                player=None, effect=None, **kwargs):
        if card is None or str(_v(card, "location", default="")).lower() != "deck":
            return False
        owner = _player_id(card)
        deck = [item for item in _candidate_cards(session, cards)
                if str(_v(item, "location", default="")).lower() == "deck" and
                _player_id(item) == owner]
        deck.sort(key=lambda item: int(_v(item, "position", default=0) or 0))
        if self.count_from_bottom:
            deck.reverse()
        if self.top_half_of_deck:
            amount = (len(deck) + 1) // 2
        else:
            amount = int(self.amount or 0)
            context = session if isinstance(session, dict) else {}
            if self.add_sapphire:
                thresholds = context.get(f"thresh_{owner}")
                if thresholds is None:
                    # Practice checkpoints use side-oriented threshold maps;
                    # target controller 0 is the AI, any human owner is the
                    # player. PvP checkpoints carry one map per raw owner.
                    thresholds = context.get(
                        "ai_threshold" if int(owner or 0) == 0 else
                        "player_threshold", {})
                amount += int(thresholds.get("Sapphire",
                             thresholds.get(2, thresholds.get("2", 0))) or 0)
            if self.add_x:
                amount += int(_v(source, "resource_x_cost_paid", "x_cost",
                                 default=0) or 0)
            if self.add_removed_counters:
                amount += (int(_v(source, "counter_cost_paid",
                                  "removed_counters", default=0) or 0) *
                           int(self.add_removed_counters_multiplier or 1))
            if self.add_source_cards_attack:
                amount += int(_v(source, "attack", "attack_value", default=0) or 0)
            if self.add_source_cards_defense:
                amount += int(_v(source, "defense", "defense_value", default=0) or 0)
            if self.add_source_cards_cost:
                amount += int(_v(source, "resource_cost", "cost", default=0) or 0)
            if self.add_card_integer_variable:
                amount += int(_source_int_attrs(source).get(
                    self.add_card_integer_variable, 0) or 0)
            if self.add_damage_dealt:
                state = _runtime_state(session)
                try:
                    from .statistics import ability_stat
                    damage = ability_stat(state, "DamageDealt")
                except (ImportError, TypeError, ValueError):
                    damage = 0
                effect_attrs = _v(
                    effect, "int_attrs", "ability_int_attrs", default={}) or {}
                if not isinstance(effect_attrs, dict):
                    effect_attrs = {}
                amount += int(effect_attrs.get("DamageDealt", damage) or 0)
            if self.add_damage_that_would_be_dealt:
                state = _runtime_state(session)
                event = (_v(effect, "trigger_event", "event", default=None) or
                         state.get("resolving_trigger_event_data") or
                         state.get("resolving_trigger_event") or {})
                amount += int(_v(event, "damage", "Damage", default=0) or 0)
        amount = max(0, amount)
        # C# first expands the positional bound for qualifying spectral cards,
        # then counts only nested-filter matches toward the bound.
        matches = [self.filter is None or self.filter.matches(
            _records_filter_card(item) if isinstance(item, dict) else item,
            source=source, player=player, session=session,
            effect=effect, **kwargs) for item in deck]
        spectral = [index for index, item in enumerate(deck)
                    if int((_v(item, "int_attrs", default={}) or {}).get(
                        "Spectral", 0) or 0) > 0 and matches[index]]
        for index in spectral:
            if index < amount:
                amount += 1
        try:
            index = next(i for i, item in enumerate(deck)
                         if item is card or _same_card(item, card))
        except StopIteration:
            return False
        if self.filter is None:
            return index < amount
        if not matches[index]:
            return False
        return sum(1 for value in matches[:index] if value) < amount

@dataclass(frozen=True)
class TACFilter:
    template: object = None
    def matches(self, card, **kwargs):
        name = self.template if isinstance(self.template, str) else _v(self.template, "name", "template_name", default=None)
        if name == "IsQuick": return IsQuick().matches(card, **kwargs)
        if name == "IsBasic": return IsBasic().matches(card, **kwargs)
        if name == "PlayerMeetsThresholdRequirementsToCast":
            player = kwargs.get("player")
            if player is None:
                return False
            return _meets_thresholds(card, player)
        return False

class PlayerMeetsThresholdRequirementsToCast:
    def matches(self, card, *, player=None, **kwargs):
        return TACFilter("PlayerMeetsThresholdRequirementsToCast").matches(card, player=player, **kwargs)

@dataclass(frozen=True)
class PlayersWhoControlMatchingFilter:
    target_filter: CardFilter | None = None
    card_collection: object = None
    required_quantity: int = 0
    comparison: object = "Equals"
    def matches(self, card, *, session=None, cards=None, **kwargs):
        if not IsChampion().matches(card):
            return False
        owner = _player_id(card)
        candidates = [(_records_filter_card(c)
                       if isinstance(c, dict) else c)
                      for c in _candidate_cards(session, cards)
                      if _player_id(c) == owner]
        if self.card_collection is not None:
            zone = InZone(cast(Any, self.card_collection))
            candidates = [c for c in candidates if zone.matches(c)]
        if self.target_filter is not None:
            candidates = [c for c in candidates if self.target_filter.matches(c, **kwargs)]
        return _cmp(len(candidates), self.comparison, int(self.required_quantity))

@dataclass(frozen=True)
class BlockingFilter:
    filter: CardFilter | None = None
    def matches(self, card, *, session=None, source=None, player=None,
                effect=None, **kwargs):
        if card is None:
            return False
        uid = int(_v(card, "card_uid", "session_card_id", "id", default=0) or 0)
        for attacker, blockers in _combat_blocker_map(session).items():
            if uid not in blockers:
                continue
            attacker_card = _combat_card(attacker, session)
            if self.filter is None or self.filter.matches(
                    attacker_card, source=source, player=player,
                    session=session, effect=effect, **kwargs):
                return True
        return False

@dataclass(frozen=True)
class BeingBlockedByFilter:
    filter: CardFilter | None = None
    def matches(self, card, *, session=None, source=None, player=None,
                effect=None, **kwargs):
        if card is None:
            return False
        uid = int(_v(card, "card_uid", "session_card_id", "id", default=0) or 0)
        blockers = _combat_blocker_map(session).get(uid, ())
        for blocker in blockers:
            blocker_card = _combat_card(blocker, session)
            if self.filter is None or self.filter.matches(
                    blocker_card, source=source, player=player,
                    session=session, effect=effect, **kwargs):
                return True
        return False


def _id(value):
    return int(_v(value, "card_uid", "session_card_id", "id", default=value)
               or 0)


def _identity(value):
    for key in ("card_uid", "session_card_id", "id"):
        if isinstance(value, dict):
            if key in value:
                try:
                    return int(value[key] or 0)
                except (TypeError, ValueError):
                    return None
        else:
            return _id(value)
    return None


def _same_card(left, right):
    if left is right:
        return True
    left_id = _identity(left)
    right_id = _identity(right)
    return left_id is not None and left_id == right_id


def _combat_blocker_map(session):
    state = _runtime_state(session) if not isinstance(session, dict) else session
    result = {}
    for key in ("ai_blockers", "player_blockers", "blockers"):
        mapping = state.get(key) or {}
        if not isinstance(mapping, dict):
            continue
        for attacker, blockers in mapping.items():
            try:
                result[_id(attacker)] = tuple(_id(value) for value in
                                              (blockers or ()))
            except (TypeError, ValueError):
                continue
    for entry in state.get("combats", ()) or ():
        if not isinstance(entry, dict):
            continue
        try:
            result[_id(entry.get("attacker_id", entry.get("attacker")))] = \
                tuple(_id(value) for value in
                      (entry.get("blocker_ids", entry.get("blockers", ())) or ()))
        except (TypeError, ValueError):
            continue
    return result


def _combat_card(uid, session):
    for card in _candidate_cards(session):
        try:
            if _id(card) == int(uid):
                return card
        except (TypeError, ValueError):
            continue
    return {"card_uid": int(uid or 0)}

class OtherTroops:
    def matches(self, card, *, source=None, effect=None, session=None, **kwargs):
        if card is None or not IsTroop().matches(card):
            return False
        instances = _v(effect, "ability_effect_instances",
                       default=None)
        if isinstance(instances, dict):
            current = _v(effect, "effect_instance_id", "instance_id",
                         default=None)
            card_id = _id(card)
            for instance_id, entry in instances.items():
                if instance_id == current:
                    continue
                targets = _v(entry, "targets", "session_card_ids",
                             "targeted_uids", default=None)
                if targets is None or card_id not in {
                        _id(value) for value in targets}:
                    return True
            return False
        target_map = _v(effect, "target_map", "targets", default=None)
        if isinstance(target_map, dict):
            card_id = _id(card)
            current = _v(effect, "effect_targets", default=()) or ()
            try:
                current_ids = {_id(value) for value in current}
            except (TypeError, ValueError):
                current_ids = set()
            for values in target_map.values():
                if isinstance(values, dict):
                    values = values.get("cards", values.get(
                        "session_card_ids", values.get("targets", ())))
                try:
                    ids = {_id(value) for value in (values or ())}
                except (TypeError, ValueError):
                    ids = set()
                if ids == current_ids:
                    continue
                if not ids or card_id not in ids:
                    return True
            return False
        targeted = set()
        state = _runtime_state(session)
        for value in state.get("targeted_uids") or ():
            try:
                targeted.add(int(value))
            except (TypeError, ValueError):
                continue
        return _id(card) not in targeted

class IsAbilitySource:
    def matches(self, card, *, source=None, effect=None, **kwargs):
        if card is None or source is None:
            return False
        source_id = _id(source)
        card_id = _id(card)
        effect_id = _v(effect, "ability_source_uid", "source_card_id",
                       default=None)
        return card is source or (effect_id is not None and
                                  card_id == effect_id) or card_id == source_id

class IsTopCard:
    def matches(self, card, *, session=None, **kwargs):
        if card is None:
            return False
        owner = _player_id(card)
        location = str(_v(card, "location", "collection", default="")).lower()
        positions = []
        for candidate in _candidate_cards(session):
            if _player_id(candidate) != owner:
                continue
            if str(_v(candidate, "location", "collection",
                       default="")).lower() != location:
                continue
            try:
                positions.append((int(_v(candidate, "position", default=0) or 0),
                                  _id(candidate)))
            except (TypeError, ValueError):
                continue
        if positions:
            return min(positions)[1] == _id(card)
        top = _v(session, "top_card", "top_card_id", default=None)
        return card is top or (top is not None and _id(card) == _id(top))

class IsTranformed:
    def matches(self, card, **kwargs):
        return bool(_v(card, "is_transformed", "transformed", "tranformed", default=False))

class IsPvECard:
    def __init__(self, is_pve=-1, **kwargs):
        self.is_pve = is_pve
    def matches(self, card, **kwargs):
        if self.is_pve < 0:
            return True
        template = _template_of(card)
        flag = bool(template.field("m_IsPvE", False)) if template is not None \
            else bool(_v(card, "is_pve", default=False))
        return (flag and self.is_pve == 1) or (not flag and self.is_pve == 0)

_MERCENARY_TEMPLATES = None
_MERCENARY_LOCK = threading.Lock()


def template_is_mercenary(template_guid):
    """Match IsMercenaryFilter against the extracted champion templates."""
    import json
    import re
    from pathlib import Path

    global _MERCENARY_TEMPLATES
    guid = str(template_guid or "").lower()
    if not guid:
        return False
    if _MERCENARY_TEMPLATES is None:
        with _MERCENARY_LOCK:
            if _MERCENARY_TEMPLATES is None:
                values = set()
                path = (Path(__file__).resolve().parents[1] / "Records" /
                        "MercenaryTemplate.jsonl")
                try:
                    with path.open(encoding="utf-8", errors="replace") as fh:
                        for line in fh:
                            try:
                                value = json.loads(line)
                                if isinstance(value, str):
                                    value = json.loads(re.sub(
                                        r",\s*([}\]])", r"\1", value))
                            except (TypeError, ValueError,
                                    json.JSONDecodeError):
                                continue
                            if not isinstance(value, dict):
                                continue
                            ident = value.get("m_Id") or {}
                            if ident.get("m_Guid"):
                                values.add(
                                    str(ident["m_Guid"]).lower())
                except OSError:
                    pass
                _MERCENARY_TEMPLATES = values
    return guid in _MERCENARY_TEMPLATES


class IsMercenaryFilter:
    def matches(self, card, **kwargs):
        if card is None or not (_card_type_value(card) & 1):
            return False
        explicit = _v(card, "is_mercenary", "mercenary", default=None)
        if template_is_mercenary(_v(card, "template_guid", "template_id",
                                   default="")):
            return True
        return bool(explicit)

@dataclass(frozen=True)
class IsEquippedCardFilter:
    equipment_type: object = "None"
    def matches(self, card, **kwargs):
        if card is None:
            return False
        explicit = _v(card, "is_equipped", "equipped", default=None)
        if explicit is not None and not _v(card, "template_guid", "template_id",
                                            default=""):
            return bool(explicit)
        try:
            from gamedata import DEFAULT_RECORD_STORE
            template = DEFAULT_RECORD_STORE.get(
                "CardTemplate", str(_v(card, "template_guid", "template_id",
                                       default="") or "").lower())
            if template is None or not bool(template.field(
                    "m_EquipmentModifiedCard", False)):
                return False
            wanted = str(self.equipment_type or "None").rsplit(".", 1)[-1]
            if wanted.lower() == "none":
                return True
            serialized = template.field("m_SerializedTAC") or {}
            data = (serialized.get("data", "")
                    if isinstance(serialized, dict) else "")
            from .tac import _tac_attr_hash, decode_tac_tree
            tree = decode_tac_tree(data)
            guid_hash = _tac_attr_hash("Guid")
            required_hash = _tac_attr_hash("RequiredEquipment")
            entries = (tree.get(required_hash)
                       if isinstance(tree, dict) else None)
            if isinstance(entries, dict):
                entries = [entries]
            for entry in entries or ():
                if not isinstance(entry, dict):
                    continue
                guid = entry.get(guid_hash)
                if not guid:
                    continue
                inventory = DEFAULT_RECORD_STORE.get(
                    "InventoryItemData", str(guid).lower())
                if inventory is None or inventory.short_type != \
                        "InventoryEquipmentData":
                    continue
                actual = str(inventory.field("m_EquipmentType", "None"))
                if actual.rsplit(".", 1)[-1].lower() == wanted.lower():
                    return True
        except (ImportError, AttributeError, TypeError, ValueError):
            pass
        return bool(_v(card, "is_equipped", "equipped", default=False))

@dataclass(frozen=True)
class IsStoredCardFilter:
    permanent_data: bool = True
    def matches(self, card, *, source=None, session=None, **kwargs):
        if card is None:
            return False
        if source is None:
            return bool(_v(card, "is_stored", "stored", default=False))
        return _id(card) in set(_stored_card_uids(session, source))

@dataclass(frozen=True)
class IsColorDeckBuilder:
    color: object = 0
    include_resources: bool = False
    prismatic: bool = False
    def matches(self, card, **kwargs):
        if card is None:
            return False
        wanted = _shard_mask(self.color)
        if not wanted:
            return False
        actual = _card_shards(card)
        flag = actual == (actual & wanted)
        if self.include_resources and not flag and _card_type_value(card) & 16:
            provided = _threshold_provided(card)
            flag = provided == (provided & wanted)
        if self.prismatic:
            flag = flag and len(_thresholds_of(card)) > 1
        return flag

@dataclass(frozen=True)
class AndCardFilter:
    filters: Sequence[CardFilter]
    def matches(self, card, **kwargs): return all(f.matches(card, **kwargs) for f in self.filters)

@dataclass(frozen=True)
class OrCardFilter:
    filters: Sequence[CardFilter]
    def matches(self, card, **kwargs): return any(f.matches(card, **kwargs) for f in self.filters)

@dataclass(frozen=True)
class NotCardFilter:
    filter: CardFilter
    def matches(self, card, **kwargs): return not self.filter.matches(card, **kwargs)

_FILTER_TYPES = {"inzone": InZone, "incollection": InCollection,
                 "istapped": IsTapped, "istype": IsType,
                 "isnottype": IsNotType,
                 "iscontrolledby": IsControlledBy,
                 "isnotcontrolledby": IsNotControlledBy,
                 "hasanyattributeflags": HasAnyAttributeFlags,
                 "hasallattributeflags": HasAllAttributeFlags,
                 "hascastingcost": HasCastingCost,
                 "hasresourcecost": HasResourceCost, "hasattackvalue": HasAttackValue,
                 "hasdefensevalue": HasDefenseValue,
                 "issocketable": IsSocketable, "issocketed": IsSocketed,
                 "haskeywordability": HasKeywordAbility,
                 "hastag": HasTag, "issubtype": IsSubType,
                 "ismultithresholdcard": IsMultiThresholdCard,
                 "hasname": HasName, "israrity": IsRarity,
                 "iscolor": IsColor, "infaction": InFaction,
                 "istoken": IsToken, "isresource": IsResource, "isquick": IsQuick,
                 "istroop": IsTroop, "isartifact": IsArtifact,
                 "ischampion": IsChampion, "ishero": IsHero,
                 "isalternateart": IsAlternateArt, "isextendedart": IsExtendedArt,
                 "ispromo": IsPromo,
                 "isplayedthisturn": IsPlayedThisTurn,
                 "isdamagedthisturn": IsDamagedThisTurn,
                 "ishealedthisturn": IsHealedThisTurn,
                 "isattacking": IsAttacking, "isblocking": IsBlocking,
                 "anycard": AnyCard, "isbasic": IsBasic,
                 "isuniquecard": IsUniqueCard, "iscardname": IsCardName,
                 "namecontains": NameContainsFilter,
                 "namecontainsfilter": NameContainsFilter,
                 "hasattackedthisturn": HasAttackedThisTurn,
                 "compareattackanddefense": CompareAttackAndDefenseFilter,
                 "compareattacktolowest": CompareAttackToLowestFilter,
                 "compareattacktohighest": CompareAttackToHighestFilter,
                 "comparedefensetolowest": CompareDefenseToLowestFilter,
                 "comparehealthtolowest": CompareHealthToLowestFilter,
                 "comparehealthtohighest": CompareHealthToHighestFilter,
                 "compareresourcecosttohighest": CompareResourceCostToHighestFilter,
                 "compareresourcecosttomyhighest": CompareResourceCostToMyHighestFilter,
                 "hassourcetype": HasSourceTypeFilter,
                 "differentowners": DifferentOwners,
                 "hasasharedfactionwithsource": HasASharedFactionWithSourceFilter,
                 "hasasharedraritywithsource": HasASharedRarityWithSourceFilter,
                 "hasasharedsubtypewithsource": HasASharedSubtypeWithSourceFilter,
                 "hasasharedclasswithsourcechampion": HasASharedClassWithSourceChampionFilter,
                 "hasasharedsubtypewithsourcechampion": HasASharedSubtypeWithSourceChampionFilter,
                 "hasasharedshardwithsource": HasASharedShardWithSourceFilter,
                 "hasasharedshardwithtopofchain": HasASharedShardWithTopOfChainFilter,
                 "movedbysource": MovedBySource,
                 "ischildofabilitysource": IsChildOfAbilitySource,
                 "isparentofabilitysource": IsParentOfAbilitySourceFilter,
                 "isparentofabilitysourcefilter": IsParentOfAbilitySourceFilter,
                 "incombatwithsource": InCombatWithSourceFilter,
                 "damagedopponentthisturn": DamagedOpponentThisTurn,
                 "hassourceresourcecost": HasSourceResourceCost,
                 "hassourcecastingcost": HasSourceCastingCostFilter,
                 "comparecastingcosttosourcecounters": CompareCastingCostToSourceCountersFilter,
                 "hascountersvalue": HasCountersValue,
                 "intattr": IntAttrFilter, "stringattr": StringAttrFilter,
                 "setid": SetIdFilter, "setnumber": SetNumberFilter,
                 "matchestarget": MatchesTargetFilter, "topnofdeck": TopNOfDeck,
                 "tac": TACFilter,
                 "playermeetsthresholdrequirementstocast": PlayerMeetsThresholdRequirementsToCast,
                 "playerswhocontrolmatching": PlayersWhoControlMatchingFilter,
                 "blocking": BlockingFilter,
                 "beingblockedby": BeingBlockedByFilter,
                 "othertroops": OtherTroops,
                 "isabilitysource": IsAbilitySource,
                 "istopcard": IsTopCard,
                 "istranformed": IsTranformed,
                 "ispvecard": IsPvECard,
                 "ismercenary": IsMercenaryFilter,
                 "isequippedcard": IsEquippedCardFilter,
                 "isstoredcard": IsStoredCardFilter,
                 "iscolor_deckbuilder": IsColorDeckBuilder,
                 "iscolordeckbuilder": IsColorDeckBuilder}


def _records_filter_spec(spec):
    """Normalize a serialized Records CardFilter into the port shape."""
    # Live Records-backed effects expose RecordObject instances while some
    # persisted/test paths provide the already-unwrapped raw dictionaries.
    # Keep the adapter boundary tolerant of both representations.
    raw = getattr(spec, "raw", None)
    if isinstance(raw, dict):
        spec = raw
    if not isinstance(spec, dict):
        return spec
    kind = str(spec.get("_t", spec.get("type", ""))).rsplit(".", 1)[-1]
    if kind in ("AndCardFilter", "OrCardFilter", "StaticAndCardFilter",
                "StaticOrCardFilter"):
        return {"type": kind, "filters": [
            _records_filter_spec(value)
            for value in spec.get("m_TargetFilters", spec.get("filters", ()))
        ]}
    if kind in ("NotCardFilter", "StaticNotCardFilter"):
        child = spec.get("m_TargetFilter", spec.get("filter", {}))
        return {"type": kind, "filter": _records_filter_spec(child)}
    result = {"type": kind}
    for key, value in spec.items():
        if key == "_t":
            continue
        result[key] = value
    return result


def records_filter_from_metadata(spec: Any) -> CardFilter:
    """Build a RulesPort filter from the client's serialized Records tree."""
    if not spec:
        return AnyCard()
    normalized: Any = _records_filter_spec(spec)
    if not isinstance(normalized, dict):
        return AnyCard()
    kind = str(normalized.get("type", "")).rsplit(".", 1)[-1].lower()
    if not kind:
        return AnyCard()
    if kind in ("andcardfilter", "orcardfilter",
                "staticandcardfilter", "staticorcardfilter"):
        children = tuple(records_filter_from_metadata(item)
                         for item in normalized.get("filters", ()))
        is_or = "or" in kind
        return OrCardFilter(children) if is_or else AndCardFilter(children)
    if kind in ("notcardfilter", "staticnotcardfilter"):
        return NotCardFilter(records_filter_from_metadata(normalized.get("filter", {})))
    if kind == "tacfilter":
        # TACFilter stores its operation in the client's compact binary blob;
        # it is not present as a normal ``template`` field in Records JSON.
        # Decode it at the Records adapter boundary so the kernel receives
        # the same semantic operation as the original client.
        serialized = normalized.get("m_SerializedTAC", {})
        data = serialized.get("data", "") if isinstance(serialized, dict) else serialized
        from .tac import tac_string
        normalized["template"] = tac_string(data, "Name")
    if kind == "istype":
        from domain.enums import card_type_from_db
        normalized["card_type"] = card_type_from_db(
            normalized.get("m_CardType", normalized.get("card_type", "")))
        normalized.pop("m_CardType", None)
    if kind == "isartifact":
        # The generic factory historically exposes the compact test/filter
        # bit used by the port tests, while serialized Records metadata uses
        # the authoritative ECardTypes.Artifact bit.  Keep that distinction
        # at the adapter boundary instead of weakening IsType globally.
        from domain.enums import ECardTypes
        return IsType(ECardTypes.Artifact)
    if kind in ("inzone", "incollection"):
        from domain.enums import ECardCollections
        collections = {name.lower(): value for name, value in vars(
            ECardCollections).items() if not name.startswith("_")}
        # Records uses both the UI name "Crypt" and the runtime name
        # "Discard" for the same collection.  Keep this translation in the
        # Records adapter so the kernel only sees canonical collection bits.
        collections.update({
            "crypt": ECardCollections.Discard,
            "crypts": ECardCollections.Discard,
            "graveyard": ECardCollections.Discard,
            "graveyards": ECardCollections.Discard,
        })
        field_name = "m_CardSource" if kind == "incollection" else "m_Collection"
        raw = str(normalized.get(field_name, normalized.get(
            "collection", ""))).rsplit(".", 1)[-1].lower()
        normalized["collection"] = collections.get(raw, 0)
        normalized.pop("m_Collection", None)
    return filter_from_metadata(normalized)


def records_filter_matches(card, spec, *, source=None, context=None,
                            player=None):
    """Evaluate one Records filter using the RulesPort card predicate set."""
    return records_filter_evaluator(
        spec, source=source, context=context, player=player)(card)


def _normalize_runtime_flags(value):
    # ``game_cards.card_state`` is the authoritative mutable card state;
    # the Records filter layer should expose the same derived predicates
    # as CardRepresentation instead of requiring every DB adapter to
    # duplicate them as booleans.
    from domain.enums import ECardStates
    if "state" in value:
        state = int(value.get("state") or 0)
        value.setdefault("is_attacking", bool(
            state & int(ECardStates.Attacking)))
        value.setdefault("is_blocking", bool(
            state & int(ECardStates.Blocking)))
        value.setdefault("tapped", bool(
            state & int(ECardStates.Tapped)))
        value.setdefault("is_tapped", value["tapped"])
        # C# derives these turn-history predicates from the same
        # authoritative bitmask (Card.CameOutThisTurn / Damaged / Healed /
        # HasAttacked).  Without them the Is*ThisTurn filters always
        # matched nothing.
        value.setdefault("played_this_turn", bool(
            state & int(ECardStates.CameOutThisTurn)))
        value.setdefault("came_out_this_turn", value["played_this_turn"])
        value.setdefault("damaged_this_turn", bool(
            state & int(ECardStates.Damaged)))
        value.setdefault("healed_this_turn", bool(
            state & int(ECardStates.Healed)))
        value.setdefault("attacked_this_turn", bool(
            state & int(ECardStates.HasAttacked)))
        value.setdefault("has_attacked_this_turn",
                         value["attacked_this_turn"])
        if (value.get("template_guid") and
                value.get("original_template_guid")):
            value.setdefault(
                "is_transformed",
                value["original_template_guid"] != value["template_guid"])
        if isinstance(value.get("shards"), (list, tuple, set)):
            value.setdefault("thresholds", list(value["shards"]))
    return value


def _records_filter_card(card, *, include_collection=True):
    """Project one runtime card dict into the Records filter card shape.

    ``include_collection`` is False for the ability-source projection: the
    source operand has never carried the derived collection bit, and folding
    it in here would change InZone predicates evaluated against the source.
    """
    from domain.enums import ECardCollections, card_type_from_db
    value = _normalize_runtime_flags(dict(card or {}))
    raw_type = value.get("card_type")
    if isinstance(raw_type, str):
        value["_card_type_name"] = raw_type
        value["card_type"] = card_type_from_db(raw_type)
        value["is_hero"] = "Champion" in raw_type.split("|")
        value["is_resource"] = "Resource" in raw_type.split("|")
    value["is_hero"] = bool(int(value.get("card_type", 0) or 0) & 1)
    value["is_resource"] = bool(int(value.get("card_type", 0) or 0) & 16)
    if "casting_cost" not in value:
        value["casting_cost"] = (0 if value["is_resource"] else
                                 int(value.get("cost", 0) or 0))
    if "user_id" in value:
        value.setdefault("owner_id", value["user_id"])
        value.setdefault("controller_id", value["user_id"])
    location = str(value.get("location", "")).lower()
    if location and include_collection:
        value["collection"] = {name.lower(): number for name, number in vars(
            ECardCollections).items() if not name.startswith("_")}.get(location, 0)
    return value


def _player_operand(value):
    """Coerce a persisted resolving-player value to a player id or ``None``.

    ``resolving_owner_id`` is written by the compatibility host, the native
    effect layer, and the legacy BOM triggers.  A card identity (template
    GUID) that reaches it is not a player, so it must not be used as a
    threshold-pool key nor raised out of filter evaluation — doing so killed
    the connection thread mid-AI-turn in a live Practice game.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        from db import log_req
        log_req("    Ignoring non-player resolving_owner_id "
                f"{value!r}; falling back to the source owner")
        return None


def _records_filter_player(player, source_value, context):
    """Resolve the player/threshold operand for one filter evaluation batch."""
    source_owner = (source_value or {}).get("controller_id")
    # A typed TAC filter needs the active player's threshold pool, not the
    # numeric owner ID.  The Records adapter receives the execution context
    # so it can expose the persisted RulesPort state without teaching the
    # generic filter classes about PvP storage keys.
    # Keep an explicitly supplied player ID intact.  Ownership predicates
    # (notably IsControlledBy) compare against this value.  Threshold-aware
    # callers that omit ``player`` still receive the client-shaped threshold
    # pool below; replacing an explicit ID with that pool makes every
    # contextual self-controlled target appear uncontrolled.
    explicit_player = player is not None
    if explicit_player:
        return player
    player = source_owner
    if context is None or player != source_owner:
        return player
    state = getattr(context, "bstate", {}) or {}
    # TAC is evaluated for the player resolving the ability.  The source
    # card's controller is normally the same player, but generated and
    # champion projections can omit or temporarily carry a different
    # controller representation.
    has_resolving_owner = "resolving_owner_id" in state
    raw_resolving_owner = state.get("resolving_owner_id")
    resolving_owner = _player_operand(raw_resolving_owner)
    active_owner = resolving_owner if has_resolving_owner else source_owner
    thresholds = (state.get(f"thresh_{active_owner}")
                  if active_owner is not None else None)
    # PvP effect execution uses the side-oriented RulesPort view rather
    # than the raw checkpoint.  Its active/opponent pools are named
    # player_threshold and ai_threshold respectively.
    if (thresholds is None and active_owner is not None and
            raw_resolving_owner and resolving_owner == active_owner):
        thresholds = state.get("player_threshold")
    if thresholds is None:
        thresholds = state.get("ai_threshold")
    normalized_thresholds = {}
    # NOTE: the loop variable must not shadow ``value`` (the card dict
    # being filtered).  It previously did, so every Records filter
    # evaluated with a threshold context compared against an int and
    # returned False — e.g. Subterranean Spy's "while underground" gate
    # (ThisIsUnderground) always failed and the hand was never revealed.
    for threshold_key, threshold_value in (thresholds or {}).items():
        try:
            threshold_key = int(threshold_key)
        except (TypeError, ValueError):
            pass
        normalized_thresholds[threshold_key] = int(threshold_value or 0)
    return {"resource_thresholds": normalized_thresholds}


def records_filter_evaluator(spec, *, source=None, context=None, player=None):
    """Compile ``spec`` once and return a reusable ``card -> bool`` predicate.

    A target scan or play-option refresh evaluates one authored filter over
    every candidate card in the scanned zones.  The compiled filter tree and
    the loop-invariant source/player/threshold operands are therefore resolved
    once per batch instead of rebuilt for each candidate.
    """
    if not spec:
        # An empty/absent filter (for example TopNOfDeck with ``m_Filter``
        # null, used by Nerissa's reveal power) matches every candidate.
        return lambda card: True
    compiled = records_filter_from_metadata(spec)
    source_value = (_records_filter_card(source, include_collection=False)
                    if source is not None else None)
    player_value = _records_filter_player(player, source_value, context)

    def matches(card):
        return compiled.matches(_records_filter_card(card),
                                source=source_value, player=player_value,
                                session=context, effect=context)
    return matches

_FILTER_PARAMETERS: dict[type, frozenset] = {}
_FILTER_PARAMETERS_LOCK = threading.Lock()


def _constructor_parameters(cls) -> frozenset:
    """Accepted keyword names for a filter class, resolved once per class."""
    parameters = _FILTER_PARAMETERS.get(cls)
    if parameters is None:
        with _FILTER_PARAMETERS_LOCK:
            parameters = _FILTER_PARAMETERS.get(cls)
            if parameters is None:
                parameters = frozenset(inspect.signature(cls).parameters)
                _FILTER_PARAMETERS[cls] = parameters
    return parameters


def filter_from_metadata(spec: Any) -> CardFilter:
    if not isinstance(spec, dict):
        raise TypeError("filter metadata must be a mapping")
    kind = str(spec.get("type", spec.get("class", spec.get("filter_type", ""))))
    kind = kind.rsplit(".", 1)[-1].replace("Filter", "").lower()
    if kind in {"and", "andcard", "staticand", "staticandcard"}:
        return AndCardFilter(tuple(filter_from_metadata(item) for item in spec.get("filters", ())))
    if kind in {"or", "orcard", "staticor", "staticorcard"}:
        return OrCardFilter(tuple(filter_from_metadata(item) for item in spec.get("filters", ())))
    if kind in {"not", "notcard", "staticnot", "staticnotcard"}:
        return NotCardFilter(filter_from_metadata(spec["filter"]))
    cls = _FILTER_TYPES.get(kind)
    if cls is None:
        raise ValueError(f"unsupported card filter type: {kind or '<missing>'}")
    values = {key: value for key, value in spec.items()
              if key not in {"type", "class", "filter_type"}}
    values = {(key[2:] if str(key).startswith("m_") else key): value
              for key, value in values.items()}
    aliases = {"card_collection": "collection", "cardcollection": "collection",
               "card_type": "card_type", "cardtype": "card_type",
               "card_name": "name", "cardname": "name",
               "contains_string": "value", "containsstring": "value",
               "includesubtype": "include_subtype",
               "includekeywords": "include_keywords",
               "languagecode": "language_code",
               "collectionflags": "collection",
               "cardsource": "collection",
               "playerfilter": "player_filter",
               "cardfilter": "card_filter",
               "targetfilter": "target_filter",
               "requiredquantity": "required_quantity",
               "equipmenttype": "equipment_type",
               "attack_value": "value", "attackvalue": "value",
               "defense_value": "value", "defensevalue": "value",
               "resource_cost": "cost", "resourcecost": "cost",
               "attribute_flags": "attribute_flags", "attributeflags": "attribute_flags",
               "casting_cost": "cost", "castingcost": "cost",
               "comparison_op": "comparison", "comparisonop": "comparison",
               "counter_type": "counter_type", "countertype": "counter_type",
               "socket_value": "socket_value", "socketvalue": "socket_value",
               "color_flags": "color", "colorflags": "color",
               "compare_to_ability_source_defense": "compare_to_ability_source_defense",
               "comparetoabilitysourcedefense": "compare_to_ability_source_defense",
               "compare_to_ability_source_attack": "compare_to_ability_source_attack",
               "comparetoabilitysourceattack": "compare_to_ability_source_attack",
               "comparetoabilitysource": "compare_to_source",
               "comparetocost": "compare_to_cost",
               "addx": "add_x",
               "addattack": "add_attack",
               "adddefense": "add_defense",
               "addcardintegervariable": "add_card_integer_variable",
               "addvariable": "add_variable",
               "addsumlistattrname": "add_sum_list_attr_name",
               "addsumproperty": "add_sum_property",
               "resourcecostcardfilter": "resource_cost_card_filter",
               "cardattributeflags": "attribute_flags",
               "usestoredname": "use_stored_name",
                "socketedvalue": "socketed_value",
                "mustbeminor": "must_be_minor",
                "alternateartpref": "alternate_art_pref",
                "hasalternateart": "has_alternate_art",
                "ispve": "is_pve",
                "extendedartonly": "extended_art_only",
                "setid": "set_id",
                "setnumber": "set_number",
                "addsapphire": "add_sapphire",
               "testagainstactiveplayer": "test_against_active_player",
               "dontexactlymatchoriginal": "dont_exactly_match_original",
               "onlycombatdamage": "only_combat_damage",
               "onlynoncombatdamage": "only_non_combat_damage",
               "usesource": "use_source",
               "addvalue": "add_value",
               "addcardintegervariable": "add_card_integer_variable",
               "addremovedcounters": "add_removed_counters",
               "addremovedcountersmultiplier": "add_removed_counters_multiplier",
               "addsourcecardsattack": "add_source_cards_attack",
               "addsourcecardsdefense": "add_source_cards_defense",
               "addsourcecardscost": "add_source_cards_cost",
               "adddamagedealt": "add_damage_dealt",
               "adddamagethatwouldbedealt": "add_damage_that_would_be_dealt",
               "tophalfofdeck": "top_half_of_deck",
               "countfrombottom": "count_from_bottom",
               "targetindex": "target_index",
               "matchname": "match_name",
               "includeresources": "include_resources",
               "exactmatch": "exact_match",
               "storedshard": "stored_shard",
               "permanentdata": "permanent_data",
               "thresholdcolorflags": "threshold_color_flags",
               "isbasicresource": "is_basic_resource",
               "isnonstandardresource": "is_non_standard_resource",
               "comparetoabilitysource": "compare_to_source",
               "comparetotriggersource": "compare_to_trigger_source",
               "comparetotriggertarget": "compare_to_trigger_target",
               "comparetosourcecontrolledcardfiltercount":
                   "compare_to_source_controlled_card_filter_count",
               "comparetocardintegervariable":
                   "compare_to_card_integer_variable",
               "comparetostoredtarget": "compare_to_stored_target",
               "failuncontrolledcards": "fail_uncontrolled_cards"}
    kwargs = {aliases.get(str(key).lower(), str(key).lower()): value
              for key, value in values.items()}
    if cls in {IsCardName, IsSocketed} and "compare_to_source" in kwargs:
        kwargs["compare_to_ability_source"] = kwargs.pop("compare_to_source")
    if (cls in {CompareAttackToLowestFilter, CompareAttackToHighestFilter,
                CompareResourceCostToHighestFilter,
                CompareResourceCostToMyHighestFilter} and
            "collection" in kwargs):
        kwargs["collection"] = _collection_bits(kwargs["collection"])
    if cls is PlayersWhoControlMatchingFilter and "collection" in kwargs:
        kwargs["card_collection"] = _collection_bits(kwargs.pop("collection"))
    nested_filter_fields = {
        CompareAttackToLowestFilter: ("card_filter",),
        CompareAttackToHighestFilter: ("card_filter",),
        CompareResourceCostToHighestFilter: ("card_filter",),
        CompareResourceCostToMyHighestFilter: ("card_filter",),
        HasAttackValue: ("compare_to_source_controlled_card_filter_count",),
        HasResourceCost: ("resource_cost_card_filter",),
        PlayersWhoControlMatchingFilter: ("target_filter",),
        BlockingFilter: ("filter",),
        BeingBlockedByFilter: ("filter",),
        TopNOfDeck: ("filter",),
    }
    for key in nested_filter_fields.get(cls, ()):
        nested = kwargs.get(key)
        if isinstance(nested, dict) or isinstance(getattr(nested, "raw", None), dict):
            kwargs[key] = records_filter_from_metadata(nested)
    for key in ("counter_type", "counter_template_guid"):
        value = kwargs.get(key)
        if isinstance(value, dict):
            kwargs[key] = value.get("m_Guid", value.get("guid", ""))
    # Several client filter classes are marker types with no constructor
    # fields.  Records retain unrelated serialized fields on those objects;
    # discard only fields not accepted by the port class.
    parameters = _constructor_parameters(cls)
    kwargs = {key: value for key, value in kwargs.items() if key in parameters}
    try:
        return cls(**kwargs)
    except TypeError as exc:
        raise ValueError(f"invalid metadata for {kind}: {exc}") from exc


def _template_card(template):
    if template is None:
        return {}

    def field(name, default=None):
        try:
            return template.field(name, default)
        except (AttributeError, TypeError):
            return default

    abilities = []
    for value in field("m_CardAbilities", []) or []:
        if isinstance(value, dict):
            ref = value.get("m_CardAbilityId", value.get("m_Guid", value))
            if isinstance(ref, dict):
                ref = ref.get("m_Guid")
            if ref:
                abilities.append(str(ref).lower())
    cost = int(field("m_ResourceCost", 0) or 0)
    return {
        "template_guid": str(getattr(template, "guid", "") or ""),
        "card_type": field("m_CardType", ""),
        "name": field("m_Name", ""),
        "subtype": field("m_CardSubtype", ""),
        "resource_cost": cost,
        "cost": cost,
        "attack": int(field("m_BaseAttackValue", 0) or 0),
        "defense": int(field("m_BaseDefenseValue", 0) or 0),
        "rarity": field("m_CardRarity", ""),
        "faction": field("m_Faction"),
        "attributes": _attribute_bits(field("m_AttributeFlags", 0)),
        "socket_count": int(field("m_SocketCount", 0) or 0),
        "thresholds": tuple(field("m_Threshold", ()) or ()),
        "set_id": _normalized_set_id(field("m_SetId")),
        "card_abilities": abilities,
        "tac_data": field("m_SerializedTAC"),
    }


_TEMPLATE_FALSE = (
    BeingBlockedByFilter, BlockingFilter, CompareAttackToHighestFilter,
    CompareAttackToLowestFilter, CompareDefenseToLowestFilter,
    CompareHealthToHighestFilter, CompareHealthToLowestFilter,
    CompareResourceCostToHighestFilter, CompareResourceCostToMyHighestFilter,
    DamagedOpponentThisTurn, DifferentOwners, HasAttackedThisTurn,
    HasCountersValue, HasTag, InCollection, InCombatWithSourceFilter, InZone,
    IsAbilitySource, IsAttacking, IsBlocking, IsChildOfAbilitySource,
    IsControlledBy, IsDamagedThisTurn, IsEquippedCardFilter, IsHealedThisTurn,
    IsNotControlledBy, IsParentOfAbilitySourceFilter, IsPlayedThisTurn,
    IsSocketed, IsStoredCardFilter, IsTapped, IsTopCard, IsTranformed,
    MatchesTargetFilter, MovedBySource, OtherTroops,
    PlayersWhoControlMatchingFilter, StringAttrFilter, TopNOfDeck,
)


def _is_basic_resource_card(card):
    return bool(_v(card, "is_basic_resource", default=
                   "standard" in str(_v(
                       card, "subtype", "subtypes", default="")).lower()))


def filter_matches_template(filter_obj, template, *, source=None, player=None,
                            session=None, effect=None, **kwargs):
    """Evaluate a ported ``CardFilter`` against a ``CardTemplate`` record."""
    if filter_obj is None or isinstance(filter_obj, _TEMPLATE_FALSE):
        return False
    if isinstance(filter_obj, AndCardFilter):
        return all(filter_matches_template(
            child, template, source=source, player=player, session=session,
            effect=effect, **kwargs) for child in filter_obj.filters)
    if isinstance(filter_obj, OrCardFilter):
        return any(filter_matches_template(
            child, template, source=source, player=player, session=session,
            effect=effect, **kwargs) for child in filter_obj.filters)
    if isinstance(filter_obj, NotCardFilter):
        return not filter_matches_template(
            filter_obj.filter, template, source=source, player=player,
            session=session, effect=effect, **kwargs)
    if isinstance(filter_obj, AnyCard):
        return True
    if isinstance(filter_obj, IsExtendedArt):
        return True
    card = _records_filter_card(_template_card(template))
    if isinstance(filter_obj, IsQuick):
        return bool(_card_type_value(card) & (64 | 8192))
    if isinstance(filter_obj, IsBasic):
        return not bool(_card_type_value(card) & (64 | 8192))
    if isinstance(filter_obj, HasCastingCost):
        return _cmp(int(_v(card, "resource_cost", default=0) or 0),
                    filter_obj.comparison, int(filter_obj.cost))
    if isinstance(filter_obj, IsResource):
        bits = _card_type_value(card)
        if not (bits & 16):
            return False
        basic = _is_basic_resource_card(card)
        if filter_obj.is_basic_resource and basic:
            return False
        if filter_obj.is_non_standard_resource and not basic:
            return False
        flag = str(filter_obj.threshold_color_flags or "Unknown").rsplit(
            ".", 1)[-1]
        if flag.lower() == "any":
            return bits == 16
        wanted = _shard_mask(filter_obj.threshold_color_flags)
        return (_threshold_provided(card) & wanted) == wanted
    return filter_obj.matches(card, source=source, player=player,
                              session=session, effect=effect, **kwargs)


def _adjust_comparison(value, comparison):
    op = str(getattr(comparison, "name", comparison)).replace(
        "_", "").replace(" ", "").lower()
    if op in ("onelessthan", "8"):
        return value - 1
    if op in ("onemorethan", "6"):
        return value + 1
    if op in ("twomorethan", "7"):
        return value + 2
    return value


def filter_cost_value(filter_obj, source_card=None, player=None, session=None,
                      effect=None, cards=None, **kwargs):
    """C# ``CardFilter.CostValue`` overrides used by random-card selection."""
    if filter_obj is None:
        return 0
    if isinstance(filter_obj, (AndCardFilter, OrCardFilter)):
        children = getattr(filter_obj, "filters", ())
        return max([filter_cost_value(
            child, source_card, player, session, effect, cards=cards,
            **kwargs) for child in children] or [0])
    if isinstance(filter_obj, HasCastingCost):
        return _adjust_comparison(int(filter_obj.cost or 0),
                                  filter_obj.comparison)
    if isinstance(filter_obj, HasResourceCost):
        return _adjust_comparison(_resource_cost_rhs(
            filter_obj, source_card, player, session, effect, **kwargs),
            filter_obj.comparison)
    if isinstance(filter_obj, CompareResourceCostToMyHighestFilter):
        responsible = _responsibility_player(player, session, source_card)
        values = [int(_v(c, "resource_cost", "cost", default=0) or 0)
                  for c in _candidate_cards(session, cards)
                  if str(_v(c, "location", default="")).lower() == "warzone"
                  and (responsible is None or _player_id(c) == responsible)]
        return _adjust_comparison(max(values) if values else 0,
                                  filter_obj.comparison)
    if isinstance(filter_obj, CompareResourceCostToHighestFilter):
        values = [int(_v(c, "resource_cost", "cost", default=0) or 0)
                  for c in _comparison_candidates(
                      source_card, collection=filter_obj.collection,
                      player_filter=filter_obj.player_filter,
                      card_filter=filter_obj.card_filter,
                      source=source_card, player=player, cards=cards,
                      session=session, effect=effect, **kwargs)]
        base = max(values) if values else 0
        return _adjust_comparison(base, filter_obj.comparison)
    if isinstance(filter_obj, CompareCastingCostToSourceCountersFilter):
        return _adjust_comparison(
            _counter_value(source_card, filter_obj.counter_type),
            filter_obj.comparison)
    if isinstance(filter_obj, HasSourceCastingCostFilter):
        selected = source_card
        if filter_obj.use_source and effect is not None:
            selected = _v(effect, "source_card", "ability_source_card",
                          "true_source", default=source_card)
        value = _casting_cost(selected)
        value += int(_v(selected, "resource_x_cost_paid", "x_cost",
                        default=0) or 0)
        value += int(filter_obj.add_value or 0)
        return value
    if isinstance(filter_obj, HasSourceResourceCost):
        value = int(_v(source_card, "resource_cost", "cost", default=0) or 0)
        return _adjust_comparison(value, filter_obj.comparison)
    if isinstance(filter_obj, IntAttrFilter):
        value = 0
        if filter_obj.compare_to_cost:
            target = source_card
            parts = [part for part in str(
                filter_obj.attribute or "").split(">") if part]
            if parts and parts[0].lower().startswith("you"):
                target = _champion_for(
                    _responsibility_player(player, session, source_card),
                    session) or target
                parts = parts[1:]
            elif parts and parts[0].lower() == "abilitytac":
                target = effect
                parts = parts[1:]
            values = _v(target, "int_attrs", "intattrs", default=None)
            if isinstance(values, dict):
                for name in parts or ():
                    value = int(values.get(name, values.get(
                        name.lower(), 0)) or 0)
                    if value:
                        break
        return _adjust_comparison(value, filter_obj.comparison)
    return 0
