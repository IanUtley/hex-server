"""Data-driven condition evaluation — Python port of the client's
Game.Shared.Mechanics.Triggers.Conditions + Abilities.Conditions, driven by
the gamedata JSON trees (ability raw_json m_TriggerCondition /
m_AbilityCondition and the seeded ability_effect_conditions table).

Unknown/unmodeled condition types default to True so they never wrongly block.
"""

import datetime
import json

import game_engine

from .targeting import (
    evaluate_card_filter,
    ZONE_MAP,
    _side_of,
    shards_from_threshold,
)


def _last(t):
    return str(t or "").split(".")[-1]


def _guid_of(obj):
    if isinstance(obj, dict):
        g = obj.get("m_Guid")
        return str(g).lower() if g else ""
    return ""


def _compare(value, op, target):
    return {
        "GreaterThanOrEqual": value >= target,
        "LessThanOrEqual": value <= target,
        "GreaterThan": value > target,
        "LessThan": value < target,
        "Equals": value == target,
    }.get(op or "GreaterThanOrEqual", True)


def _side_of(user_id):
    return "ai" if not user_id else "player"


def _filter_zones(node):
    """Return exact InZone collections contained in a card-filter tree."""
    if isinstance(node, dict):
        result = set()
        if _last(node.get("_t")) == "InZone" and node.get("m_Collection"):
            collection = str(node["m_Collection"])
            result.add(ZONE_MAP.get(collection, collection.lower()))
        for child in node.values():
            result.update(_filter_zones(child))
        return result
    if isinstance(node, list):
        result = set()
        for child in node:
            result.update(_filter_zones(child))
        return result
    return set()


class ConditionContext:
    """Everything the evaluator needs about the current trigger/ability."""

    def __init__(self, db, session, bstate, event_type=None,
                 ability_source_uid=None, ability_source_owner_id=None,
                 trigger_uid=None, pl_t=None, ai_t=None, extra_target=None,
                 champions=None, ability_source_card_owner=None,
                 trigger_owner_id=None, event_source_collection=None,
                 event_destination_collection=None, event_previous_state=None,
                 uses_previous_state=False):
        self.db = db
        self.session = session
        self.bstate = bstate or {}
        self.event_type = event_type
        self.ability_source_uid = ability_source_uid
        self.ability_source_owner_id = ability_source_owner_id
        # The event's responsible player is separate from the controller of
        # the card whose trigger is being evaluated.  For CardDrawnEvent this
        # is the drawer, which is what "When you draw" must compare against.
        self.trigger_owner_id = trigger_owner_id
        # CardEnteredZoneEvent is evaluated after the DB move, so retain the
        # event's pre-move collection/state explicitly.  The client uses this
        # same previous-state data for authored triggers such as "dies".
        self.event_source_collection = event_source_collection
        self.event_destination_collection = event_destination_collection
        self.event_previous_state = event_previous_state
        self.uses_previous_state = bool(uses_previous_state)
        # The ability SOURCE CARD's actual owner (its game_cards.user_id) —
        # distinct from the EVENT's source owner.  IsControlledBy /
        # IsNotControlledBy card filters must compare against the card that
        # OWNS the ability (e.g. Incantation of Fear's "a card enters an
        # OPPOSING crypt": the entering AI card must not be controlled by the
        # player's Incantation).  Defaults to the event source owner for
        # callers that don't distinguish the two.
        self._src_side = _side_of(
            ability_source_card_owner
            if ability_source_card_owner is not None
            else ability_source_owner_id)
        self.trigger_uid = trigger_uid
        self.extra_target = extra_target
        self.pl_t = pl_t
        self.ai_t = ai_t
        self._cards = {}
        self.ability_variables = {}
        # Per-effect-instance "was applied" map (the authoritative resolver
        # fills it in) so NotContingentAbilityCondition ("Otherwise", e.g.
        # Spawn of Othuyeg's "if ten or more cards in opposing crypts, bury
        # five; otherwise bury one") can gate on a sibling effect.
        self.applied_effects = {}
        # Champions are not game_cards rows in live battles — the handler's
        # _champion_targets() provides (uid, user_id, name, health) tuples so
        # IsHero filters and "controls target" conditions can evaluate them.
        self.champions = champions or []
        self._champ_by_uid = {}
        for _c_uid, _c_owner, _c_name, _c_hp in self.champions:
            try:
                self._champ_by_uid[int(_c_uid)] = (_c_owner, _c_name, _c_hp)
            except (TypeError, ValueError):
                continue

    def card(self, card_uid):
        if card_uid is None:
            return None
        key = int(card_uid)
        if key in self._champ_by_uid:
            owner, name, hp = self._champ_by_uid[key]
            return {
                "card_uid": key,
                "card_type": "Champion",
                "location": "champion",
                "user_id": owner,
                "state": 0,
                "attack": 0,
                "defense": int(hp or 0),
                "template_guid": "",
                "name": name or "Champion",
                "cost": 0,
                "subtype": "",
                "shards": [],
                "attributes": 0,
                "damaged_opponent_this_turn": list(
                    (self.bstate or {}).get("damaged_opponent_this_turn") or []),
                "src_owner_side": self._src_side,
            }
        if key not in self._cards:
            row = self.db.execute(
                "SELECT gc.card_uid, COALESCE(gc.card_type, ct.card_type), "
                "gc.location, gc.user_id, gc.card_state, "
                "COALESCE(ct.attack,0), COALESCE(ct.defense,0), "
                "gc.template_guid, ct.name, COALESCE(ct.cost,0), "
                "ct.subtype, ct.threshold_json, gc.card_attributes, ct.attributes "
                "FROM game_cards gc LEFT JOIN card_templates ct "
                "ON ct.guid = gc.template_guid "
                "WHERE gc.session_id=? AND gc.card_uid=?",
                (self.session.session_id, key)).fetchone()
            if row:
                self._cards[key] = {
                    # Keep the event card visible even if a legacy/fallback
                    # game-card row has no matching template.  Conditions
                    # such as IsSubType(Spider) must then fail closed from
                    # the available card_type/subtype data; returning None
                    # here made every typed trigger condition default True.
                    "card_uid": int(row[0]), "card_type": row[1] or "",
                    "location": row[2], "user_id": row[3],
                    "state": int(row[4] or 0), "attack": row[5],
                    "defense": row[6], "template_guid": row[7],
                    "name": row[8] or "", "cost": row[9] or 0,
                    "subtype": row[10] or "",
                    "shards": shards_from_threshold(row[11]),
                    "attributes": int(row[12] or 0) | int(row[13] or 0),
                    "damaged_opponent_this_turn": list(
                        (self.bstate or {}).get("damaged_opponent_this_turn") or []),
                    "src_owner_side": self._src_side,
                }
            else:
                self._cards[key] = None
        return self._cards[key]

    def _zones(self, flags):
        return {ZONE_MAP.get(z, z.lower())
                for z in (flags or "").split("|") if z}

    def _cards_in_zones(self, zones, user_id=None):
        sql = ("SELECT gc.card_uid, gc.card_type, gc.location, gc.user_id, "
               "gc.card_state, COALESCE(ct.attack,0), COALESCE(ct.defense,0), "
               "ct.name, COALESCE(ct.cost,0), ct.subtype, ct.threshold_json, "
               "gc.card_attributes, ct.attributes "
               "FROM game_cards gc JOIN card_templates ct ON ct.guid = gc.template_guid "
               "WHERE gc.session_id=? AND gc.location IN (%s)"
               % ",".join("?" * len(zones)))
        params = [self.session.session_id] + list(zones)
        if user_id is not None:
            sql += " AND gc.user_id=?"
            params.append(user_id)
        out = []
        for r in self.db.execute(sql, params):
            out.append({"card_uid": int(r[0]), "card_type": r[1],
                        "location": r[2], "user_id": r[3],
                        "state": int(r[4] or 0), "attack": r[5],
                        "defense": r[6], "name": r[7] or "",
                        "cost": r[8] or 0,
                        "subtype": r[9] or "",
                        "shards": shards_from_threshold(r[10]),
                        "attributes": int(r[11] or 0) | int(r[12] or 0),
                        "damaged_opponent_this_turn": list(
                            (self.bstate or {}).get("damaged_opponent_this_turn") or []),
                        "src_owner_side": self._src_side})
        return out

    def _counter_count(self, card, counter_guid):
        """Count a card's counters by its gamedata counter template GUID."""
        if not card:
            return 0
        try:
            row = self.db.execute(
                "SELECT name FROM card_counter_templates WHERE template_id=?",
                (counter_guid,)).fetchone()
        except Exception:
            return 0
        if not row:
            return 0
        name = row[0]
        prow = self.db.execute(
            "SELECT permanent_buffs FROM game_cards WHERE session_id=? AND card_uid=?",
            (self.session.session_id, card["card_uid"])).fetchone()
        try:
            data = json.loads((prow[0] if prow else "{}") or "{}")
            counters = data.get("counters") or {}
            return int(counters.get((name or "").lower(), 0) or 0)
        except Exception:
            return 0


def evaluate_condition(node, ctx):
    """Evaluate one condition tree node (dict from gamedata JSON)."""
    if not isinstance(node, dict):
        return True
    t = _last(node.get("_t"))

    # --- combinators -----------------------------------------------------
    if t in ("AndTriggerCondition", "AndEffectCondition", "AndAbilityCondition"):
        return all(evaluate_condition(c, ctx)
                   for c in (node.get("m_Conditions") or []))
    if t in ("OrTriggerCondition", "OrEffectCondition"):
        return any(evaluate_condition(c, ctx)
                   for c in (node.get("m_Conditions") or []))
    if t in ("NotTriggerCondition", "NotEffectCondition"):
        inner = node.get("m_Condition")
        if isinstance(inner, dict):
            return not evaluate_condition(inner, ctx)
        conds = node.get("m_Conditions") or []
        return not evaluate_condition(conds[0], ctx) if conds else True

    # --- trigger conditions ----------------------------------------------
    if t == "TriggerCardIsAbilitySource":
        return (ctx.trigger_uid is not None
                and int(ctx.trigger_uid) == int(ctx.ability_source_uid or 0))
    if t == "TriggerPlayerControlsAbilitySource":
        # Client semantics: the trigger PLAYER (per m_TriggerTest, default the
        # event's source player) must control the ability source card.  For
        # CardDrawnEvent the source player is the drawer, so a both-sides
        # gather plus this gate lets "when you draw" vs "when an opposing
        # champion draws" fire on the correct side only.
        card = ctx.card(ctx.ability_source_uid)
        if card is None:
            return True
        trigger_owner = (ctx.trigger_owner_id
                         if ctx.trigger_owner_id is not None
                         else ctx.ability_source_owner_id)
        if ctx.bstate.get("pvp"):
            return int(card["user_id"]) == int(trigger_owner or 0)
        return (_side_of(card["user_id"]) == _side_of(trigger_owner))
    if t == "TriggerAbilityIsChargePower":
        # CardActivatedEvent carries the activated template in the transient
        # battle state.  Champion abilities and granted talent abilities use
        # separate seed tables, so check both without relying on card text.
        activated = (ctx.bstate or {}).get("activated_ability_guid")
        if not activated:
            return False
        for table in ("champion_abilities", "talent_abilities"):
            try:
                row = ctx.db.execute(
                    "SELECT charge_cost FROM %s WHERE ability_guid=? "
                    "LIMIT 1" % table, (str(activated).lower(),)).fetchone()
            except Exception:
                row = None
            if row is not None:
                return int(row[0] or 0) > 0
        return False
    if t == "TriggerPlayerControlsCard":
        card = ctx.card(ctx.trigger_uid)
        if card is None:
            return True
        return _side_of(card["user_id"]) == _side_of(ctx.ability_source_owner_id)
    if t == "TriggerPlayerControlsTarget":
        card = ctx.card(ctx.extra_target)
        if card is None:
            return True
        return _side_of(card["user_id"]) == _side_of(ctx.ability_source_owner_id)
    if t == "TriggerCardMatchesFilter":
        # The client's TriggerCondition tests either the event's SOURCE card
        # (TriggerSource — for CardDrawnEvent the drawing champion), its TARGET
        # card (TriggerTarget — the drawn card), or the source player's
        # champion (TriggerSourcePlayer).  Fall back to the trigger source when
        # no target was supplied (events without a TargetCardId).
        test = node.get("m_TriggerTest") or "TriggerSource"
        if test == "TriggerTarget":
            uid = (ctx.extra_target if ctx.extra_target is not None
                   else ctx.trigger_uid)
        elif test == "TriggerSourcePlayer":
            uid = ctx.trigger_uid
        else:
            uid = ctx.trigger_uid
        card = ctx.card(uid)
        if card is None:
            return True
        return evaluate_card_filter(card, node.get("m_CardFilter"),
                                    ctx.ability_source_uid)
    if t == "TriggerCardEnteredZone":
        card = ctx.card(ctx.trigger_uid)
        if card is None:
            return True
        source = ctx.event_source_collection
        destination = ctx.event_destination_collection
        # Older callers did not carry event metadata. Preserve their
        # destination fallback, while metadata-aware callers are evaluated
        # against the actual transition rather than the post-move DB row.
        if destination is None:
            destination = card["location"]
        zones = ctx._zones(node.get("m_DestinationCollection", ""))
        if zones and destination not in zones:
            return False
        source_zones = ctx._zones(node.get("m_SourceCollection", ""))
        if source_zones and source is not None and source not in source_zones:
            return False
        # In the authored data, m_UsesPreviousState marks the Warzone ->
        # Discard triggers whose meaning is a troop dying. The separate
        # crypt-entry triggers intentionally leave this flag unset. Require
        # the transient Dead bit from the pre-move state so cards buried from
        # hand/deck cannot masquerade as deaths.
        if (ctx.uses_previous_state and source_zones
                and source in ctx._zones("Warzone")
                and destination in ctx._zones("Discard")):
            previous_state = ctx.event_previous_state
            if previous_state is None:
                previous_state = card.get("state", 0)
            if not (int(previous_state or 0) & game_engine.ECardStates.Dead):
                return False
        your = int(node.get("m_Your", 0) or 0)
        opposing = int(node.get("m_Opposing", 0) or 0)
        on_side = _side_of(card["user_id"]) == _side_of(ctx.ability_source_owner_id)
        if your and not on_side:
            return False
        if opposing and on_side:
            return False
        return True
    if t == "TriggerCardIsNthCardDrawnThisTurnByThisPlayer":
        nth = int(node.get("m_Nth", 1) or 1)
        side = _side_of(ctx.ability_source_owner_id)
        drawn = int(ctx.bstate.get(f"{side}_draws_this_turn", 0))
        return drawn == nth
    if t == "TriggerPlayerIsActivePlayer":
        return ctx.bstate.get("turn_player") == _side_of(ctx.ability_source_owner_id)
    if t == "TriggerCardSameNameInZone":
        card = ctx.card(ctx.ability_source_uid)
        if not card or not card.get("template_guid"):
            return True
        zones = ctx._zones(node.get("m_Collection", "")
                           or node.get("m_CollectionFlags", ""))
        if not zones:
            return True
        rows = ctx.db.execute(
            "SELECT 1 FROM game_cards WHERE session_id=? AND template_guid=? "
            "AND location IN (%s) LIMIT 1"
            % ",".join("?" * len(zones)),
            [ctx.session.session_id, card["template_guid"]] + list(zones)).fetchone()
        return bool(rows)
    if t == "TriggerCardIsStoredTargetOfAbilitySource":
        if ctx.trigger_uid is None:
            return False
        all_stored = [u for v in ((ctx.bstate or {}).get("stored_targets") or {}).values()
                      for u in v]
        return int(ctx.trigger_uid) in {int(u) for u in all_stored}
    if t == "TriggerCardCounter":
        card = ctx.card(ctx.ability_source_uid)
        cguid = _guid_of(node.get("m_CardCounterTemplateId"))
        req = int(node.get("m_RequiredCount", 1) or 1)
        return ctx._counter_count(card, cguid) >= req if card else True
    if t == "TriggerPlayerHealth":
        val = int(node.get("m_Health", 0) or 0)
        op = node.get("m_ComparisonOp", "GreaterThanOrEqual")
        side = _side_of(ctx.ability_source_owner_id)
        hp = int(ctx.bstate.get(f"{side}_health", 20))
        return _compare(hp, op, val)
    if t == "ChampionActionsCastThisTurn":
        side = _side_of(ctx.ability_source_owner_id)
        count = int(ctx.bstate.get(f"{side}_actions_cast_this_turn", 0))
        req = int(node.get("m_RequiredQuantity", 1) or 1)
        return _compare(count, node.get("m_ComparisonOp", "GreaterThanOrEqual"),
                        req)

    # --- ability / effect conditions --------------------------------------
    if t == "AbilityControllerHasThresholdAbilityCondition":
        color = (node.get("m_ColorFlags", "") or "").lower()
        need = int(node.get("m_RequiredQuantity", 1) or 1)
        owner_id = int(ctx.ability_source_owner_id or 0)
        side = _side_of(owner_id)
        flag = game_engine.SHARD_TO_FLAG.get(color, 0)
        if not flag:
            return True
        # Practice stores thresholds in player_threshold/ai_threshold. PvP
        # persists the same values under thresh_<pid>; ability conditions are
        # shared by both paths and must read the authoritative representation.
        thresholds = ctx.bstate.get(f"{side}_threshold", {}) or {}
        if ctx.bstate.get("pvp"):
            thresholds = ctx.bstate.get(f"thresh_{owner_id}", {}) or {}
        have = thresholds.get(flag)
        if have is None:
            have = thresholds.get(str(flag), 0)
        have = int(have or 0)
        return have >= need
    if t in ("AbilityControllerIsActiveAbilityCondition",
             "AbilityControllerHasPriorityAbilityCondition"):
        return ctx.bstate.get("turn_player") == _side_of(ctx.ability_source_owner_id)
    if t == "SourceCardHasCounters":
        card = ctx.card(ctx.ability_source_uid)
        cguid = _guid_of(node.get("m_CardCounterTemplateId"))
        req = int(node.get("m_RequiredCounters", 1) or 1)
        op = node.get("m_ComparisonOp", "GreaterThanOrEqual")
        count = ctx._counter_count(card, cguid) if card else 0
        return _compare(count, op, req)
    if t == "RequiresCardsControlled":
        zones = ctx._zones(node.get("m_CardCollection", ""))
        if not zones:
            return True
        req = int(node.get("m_RequiredQuantity", 1) or 1)
        op = node.get("m_ComparisonOp", "GreaterThanOrEqual")
        fjson = node.get("m_CardFilter") or {}
        pfilter = (node.get("m_PlayerFilter") or "Self")
        src_side = _side_of(ctx.ability_source_owner_id)
        # All selected PreGame abilities must see the deck before any of the
        # other PreGame abilities insert cards.  The setup pass snapshots the
        # count per owner for this exact deck-count condition.
        if pfilter in ("Self", "You", "Controller") and \
                _filter_zones(fjson) == {"deck"}:
            snapshots = (ctx.bstate or {}).get(
                "pregame_initial_deck_counts") or {}
            snapshot = snapshots.get(str(ctx.ability_source_owner_id))
            if snapshot is None:
                snapshot = snapshots.get(ctx.ability_source_owner_id)
            if snapshot is not None:
                return _compare(int(snapshot), op, req)
        count = 0
        for card in ctx._cards_in_zones(zones):
            side = _side_of(card["user_id"])
            if pfilter in ("Self", "You", "Controller") and side != src_side:
                continue
            if pfilter in ("Opposing", "Opponents", "MultipleOpponents") \
                    and side == src_side:
                continue
            if evaluate_card_filter(card, fjson, ctx.ability_source_uid):
                count += 1
        return _compare(count, op, req)
    if t == "RequiresDateTime":
        now = datetime.datetime.now()
        values = {
            "m_Year": now.year,
            "m_Month": now.month,
            "m_Day": now.day,
            # .NET DateTime.DayOfWeek is Sunday=0; Python weekday is Monday=0.
            "m_DayOfWeek": (now.weekday() + 1) % 7,
            "m_DayOfYear": now.timetuple().tm_yday,
            "m_Hour": now.hour,
            "m_Minute": now.minute,
            "m_Second": now.second,
        }
        op = node.get("m_ComparisonOp", "Equals")
        for field, actual in values.items():
            try:
                expected = int(node.get(field, -1))
            except (TypeError, ValueError):
                expected = -1
            if expected >= 0 and not _compare(actual, op, expected):
                return False
        return True
    if t == "CardFilterAbilityCondition":
        zones = ctx._zones(node.get("m_CollectionFlags", "")
                           or node.get("m_CardCollection", ""))
        if not zones:
            return True
        fjson = node.get("m_CardFilter") or {}
        return any(evaluate_card_filter(card, fjson, ctx.ability_source_uid)
                   for card in ctx._cards_in_zones(zones))
    if t == "RequiresSourcePassesFilterCondition":
        card = ctx.card(ctx.ability_source_uid)
        if card is None:
            return True
        return evaluate_card_filter(card, node.get("m_Filter") or {},
                                    ctx.ability_source_uid)
    if t in ("RequiresChampionHealth", "RequiresChampionCharges",
             "RequiresResourceThreshold", "RequiresTotalResources"):
        side = _side_of(ctx.ability_source_owner_id)
        key = {"RequiresChampionHealth": f"{side}_health",
               "RequiresChampionCharges": f"{side}_charges",
               "RequiresTotalResources": f"{side}_total_resources",
               "RequiresResourceThreshold": None}.get(t)
        if key is None:
            return True
        val = int(ctx.bstate.get(key, 0))
        target = int(node.get("m_RequiredQuantity", node.get("m_Value", 0)) or 0)
        op = node.get("m_ComparisonOp", "GreaterThanOrEqual")
        return _compare(val, op, target)

    if t == "AbilityVariableCondition":
        # "if RandomNumber == 1" — the variable was set by a
        # RandomizeVariable / SetAbilityVariable effect earlier in the BOM.
        lhs = str(node.get("m_Lhs") or "")
        rhs = str(node.get("m_Rhs") or "")
        if lhs not in ctx.ability_variables:
            return False
        op = node.get("m_ComparisonOp", "Equals")
        return _compare(int(ctx.ability_variables[lhs]),
                        op, int(rhs))

    if t in ("NotContingentAbilityCondition", "NotContingentEffectCondition"):
        # "Otherwise N": true when the referenced effect instance did NOT
        # apply earlier in this ability's resolution.
        idx = node.get("m_EffectIndex")
        if idx is None:
            return True
        try:
            return not bool(ctx.applied_effects.get(int(idx), False))
        except (TypeError, ValueError):
            return True

    # Unmodeled condition types never wrongly block.
    return True


def evaluate_effect_condition(db, condition_id, ctx):
    """Evaluate a BOM leaf's condition (AbilityEffectConditionTemplate)."""
    if not condition_id:
        return True
    try:
        row = db.execute(
            "SELECT condition_json FROM ability_effect_conditions "
            "WHERE condition_id=?", (condition_id,)).fetchone()
    except Exception:
        return True
    if not row or not row[0]:
        return True
    try:
        node = json.loads(row[0])
    except Exception:
        return True
    return evaluate_condition(node, ctx)


def trigger_condition_met(raw_json, ctx):
    """Evaluate the ability's m_AbilityCondition + m_TriggerCondition trees
    from its raw_json.  Returns True when both hold (or are absent)."""
    if not raw_json:
        return True
    try:
        rec = json.loads(raw_json)
    except Exception:
        return True
    if not isinstance(rec, dict):
        return True
    ab = rec.get("m_AbilityCondition")
    if isinstance(ab, dict) and not evaluate_condition(ab, ctx):
        return False
    trig = rec.get("m_TriggerCondition")
    if isinstance(trig, dict) and not evaluate_condition(trig, ctx):
        return False
    return True
