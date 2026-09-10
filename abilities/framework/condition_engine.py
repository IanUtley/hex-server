"""Data-driven condition evaluation — Python port of the client's
Game.Shared.Mechanics.Triggers.Conditions + Abilities.Conditions, driven by
the gamedata JSON trees (ability raw_json m_TriggerCondition /
m_AbilityCondition and the seeded ability_effect_conditions table).

Unknown/unmodeled condition types default to True so they never wrongly block.
"""

import datetime
import json
from collections.abc import Mapping

import game_engine

from .targeting import (
    evaluate_card_filter,
    ZONE_MAP,
    _side_of,
    shards_from_threshold,
    template_faction,
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
                 uses_previous_state=False, event_int_attribute=None,
                 event_tac=None):
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
        self.event_int_attribute = event_int_attribute
        # Trigger events carry a transient TAC in the original client.  Keep
        # it on the evaluation context rather than mutating the shared battle
        # state, since nested triggers can otherwise overwrite one another's
        # event payload.
        self.event_tac = event_tac or {}
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
            counters = self._champion_counter_counts(key)
            return {
                "card_uid": key,
                "card_type": "Champion",
                "location": "champions",
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
                "counters": counters,
                "counter_guids": {guid: guid for guid in counters},
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
                "ct.subtype, ct.threshold_json, gc.card_attributes, ct.attributes, "
                "gc.card_attack_mod, gc.card_defense_mod, "
                "COALESCE(gc.permanent_buffs,'{}') "
                "FROM game_cards gc LEFT JOIN card_templates ct "
                "ON ct.guid = gc.template_guid "
                "WHERE gc.session_id=? AND gc.card_uid=?",
                (self.session.session_id, key)).fetchone()
            if row:
                base_atk = int(row[5] or 0)
                base_def = int(row[6] or 0)
                # Persistent stat modifiers survive a move to the discard and
                # are therefore part of the previous-state value used by
                # death-trigger conditions.  Temporary combat/zone buffs are
                # intentionally excluded because the death path clears them.
                try:
                    permanent = json.loads(row[16] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    permanent = {}
                atk = (base_atk + int(row[14] or 0)
                       + int(permanent.get("atk", 0) or 0))
                defense = (base_def + int(row[15] or 0)
                           + int(permanent.get("def", 0) or 0))
                counters, counter_guids = self._game_card_counter_counts(key)
                self._cards[key] = {
                    # Keep the event card visible even if its runtime template
                    # row is absent. Conditions such as IsSubType(Spider)
                    # then fail closed from the available card_type/subtype
                    # data; returning None would default every condition True.
                    "card_uid": int(row[0]), "card_type": row[1] or "",
                    "location": row[2], "user_id": row[3],
                    "state": int(row[4] or 0), "attack": atk,
                    "defense": defense, "template_guid": row[7],
                    "name": row[8] or "", "cost": row[9] or 0,
                    "subtype": row[10] or "",
                    "faction": template_faction(row[7]),
                    "shards": shards_from_threshold(row[11]),
                    "attributes": int(row[12] or 0) | int(row[13] or 0),
                    "int_attrs": (permanent.get("int_attrs", {})
                                  if isinstance(permanent.get("int_attrs", {}), dict)
                                  else {}),
                    "counters": counters,
                    "counter_guids": counter_guids,
                    "damaged_opponent_this_turn": list(
                        (self.bstate or {}).get("damaged_opponent_this_turn") or []),
                    "src_owner_side": self._src_side,
                }
            else:
                self._cards[key] = None
        return self._cards[key]

    def _champion_counter_counts(self, card_uid):
        """Read a champion's typed counters from persisted battle state."""
        values = ((self.bstate or {}).get("champion_counters") or {}).get(
            str(int(card_uid)), {})
        if not isinstance(values, dict):
            return {}
        out = {}
        for guid, count in values.items():
            try:
                if int(count or 0) > 0:
                    out[str(guid).lower()] = int(count)
            except (TypeError, ValueError):
                continue
        return out

    def _game_card_counter_counts(self, card_uid):
        """Return a game card's persisted counter names and GUIDs."""
        try:
            row = self.db.execute(
                "SELECT permanent_buffs FROM game_cards WHERE session_id=? "
                "AND card_uid=?", (self.session.session_id, int(card_uid))
            ).fetchone()
            data = json.loads((row[0] if row else "{}") or "{}")
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        counters = data.get("counters")
        guids = data.get("counter_guids")
        return (counters if isinstance(counters, dict) else {},
                guids if isinstance(guids, dict) else {})

    def _zones(self, flags):
        return {ZONE_MAP.get(z, z.lower())
                for z in (flags or "").split("|") if z}

    def _cards_in_zones(self, zones, user_id=None):
        sql = ("SELECT gc.card_uid, gc.card_type, gc.location, gc.user_id, "
               "gc.card_state, COALESCE(ct.attack,0), COALESCE(ct.defense,0), "
               "ct.name, COALESCE(ct.cost,0), ct.subtype, ct.threshold_json, "
               "gc.card_attributes, ct.attributes, gc.template_guid, "
               "COALESCE(gc.permanent_buffs,'{}') "
               "FROM game_cards gc JOIN card_templates ct ON ct.guid = gc.template_guid "
               "WHERE gc.session_id=? AND gc.location IN (%s)"
               % ",".join("?" * len(zones)))
        params = [self.session.session_id] + list(zones)
        if user_id is not None:
            sql += " AND gc.user_id=?"
            params.append(user_id)
        out = []
        for r in self.db.execute(sql, params):
            counters, counter_guids = self._game_card_counter_counts(r[0])
            try:
                saved = json.loads(r[14] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                saved = {}
            int_attrs = saved.get("int_attrs", {}) if isinstance(saved, dict) else {}
            if not isinstance(int_attrs, dict):
                int_attrs = {}
            out.append({"card_uid": int(r[0]), "card_type": r[1],
                        "location": r[2], "user_id": r[3],
                        "state": int(r[4] or 0), "attack": r[5],
                        "defense": r[6], "name": r[7] or "",
                        "cost": r[8] or 0,
                        "subtype": r[9] or "",
                        "shards": shards_from_threshold(r[10]),
                        "attributes": int(r[11] or 0) | int(r[12] or 0),
                        "faction": template_faction(r[13]),
                        "int_attrs": int_attrs,
                        "counters": counters,
                        "counter_guids": counter_guids,
                        "damaged_opponent_this_turn": list(
                            (self.bstate or {}).get("damaged_opponent_this_turn") or []),
                        "src_owner_side": self._src_side})
        # Champions are session cards, not game_cards rows.  Include them for
        # Conditions.RequiresCardsControlled(Champions), including typed
        # HasCountersValue filters such as Squashing Pumpkins' win condition.
        if "champions" in zones:
            for c_uid, c_owner, c_name, c_hp in self.champions:
                if user_id is not None and int(c_owner or 0) != int(user_id):
                    continue
                counters = self._champion_counter_counts(c_uid)
                out.append({
                    "card_uid": int(c_uid), "card_type": "Champion",
                    "location": "champions", "user_id": c_owner,
                    "state": 0, "attack": 0, "defense": int(c_hp or 0),
                    "name": c_name or "Champion", "cost": 0,
                    "subtype": "", "shards": [], "attributes": 0,
                    "counters": counters,
                    "counter_guids": {guid: guid for guid in counters},
                    "damaged_opponent_this_turn": list(
                        (self.bstate or {}).get(
                            "damaged_opponent_this_turn") or []),
                    "src_owner_side": self._src_side,
                    "src_owner_id": self.ability_source_owner_id,
                })
        return out

    def _counter_count(self, card, counter_guid):
        """Count a card's counters by its gamedata counter template GUID."""
        if not card:
            return 0
        wanted = str(counter_guid or "").lower()
        counters = card.get("counters") or {}
        guids = card.get("counter_guids") or {}
        if counters:
            total = 0
            for name, count in counters.items():
                if str(name).lower() == wanted or \
                        str(guids.get(name, "")).lower() == wanted:
                    try:
                        total += int(count or 0)
                    except (TypeError, ValueError):
                        pass
            return total
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
        return evaluate_card_filter(
            card, node.get("m_CardFilter"), ctx.ability_source_uid,
            source_card=ctx.card(ctx.ability_source_uid))
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
    if t == "TriggerEventIsCombatDamage":
        # The combat resolver emits CardDealtDamageEvent. Ability damage uses
        # CardWouldBeDamagedEvent and must not satisfy this condition. A
        # replacement event carries the original combat flag in its event
        # TAC, matching the client's CardWouldDealDamageEvent.IDamage data.
        if _last(ctx.event_type) == "CardDealtDamageEvent":
            return True
        return (_last(ctx.event_type) == "CardWouldDealDamageEvent" and
                bool((ctx.event_tac or {}).get("is_combat_damage")))
    if t == "TriggerEventIntAttribute":
        return (_last(ctx.event_type) == "CardGainedIntAttrEvent" and
                str(node.get("m_Attribute") or "") == str(
                    ctx.event_int_attribute or ""))
    if t == "TurnPhaseCondition":
        wanted = str(node.get("m_TurnPhase") or "")
        try:
            wanted_value = int(getattr(game_engine.ETurnPhases, wanted))
        except (AttributeError, TypeError, ValueError):
            wanted_value = None
        current = (ctx.bstate or {}).get("phase")
        if current is None:
            try:
                import battle_engine
                current = battle_engine.current_phase(ctx.bstate)
            except Exception:
                current = None
        return (str(current) == wanted or
                (wanted_value is not None and int(current or -1) == wanted_value))
    if t == "CardsDiscardedThisTurn":
        owner = ctx.ability_source_owner_id
        if (ctx.bstate or {}).get("pvp"):
            value = int(ctx.bstate.get(
                f"cards_discarded_this_turn_{int(owner or 0)}", 0) or 0)
        else:
            value = int(ctx.bstate.get(
                f"{_side_of(owner)}_cards_discarded_this_turn", 0) or 0)
        required = int(node.get("m_RequiredQuantity", node.get(
            "m_Amount", node.get("m_Value", 1))) or 1)
        return _compare(value, node.get("m_ComparisonOp", "GreaterThanOrEqual"),
                        required)
    if t == "IntAttrFilter":
        attr_name = str(node.get("m_Attribute") or "")
        if attr_name.startswith("AbilityTAC>"):
            from .tac import _tac_attr_hash
            actual = int((ctx.event_tac or {}).get(
                _tac_attr_hash(attr_name.split(">", 1)[1]), 0) or 0)
            rhs = int(node.get("m_Value", 0) or 0)
            return _compare(actual, node.get("m_ComparisonOp", "Equals"), rhs)
        target = ctx.card(ctx.trigger_uid) or ctx.card(ctx.ability_source_uid)
        return evaluate_card_filter(target, node, ctx.ability_source_uid) \
            if target is not None else True
    if t == "TACTriggerCondition":
        serialized = node.get("m_Conditions") or {}
        data = serialized.get("data") if isinstance(serialized, dict) else None
        if not data:
            return True
        try:
            from .tac import decode_tac_tree, _tac_attr_hash
            required = decode_tac_tree(data)
        except (TypeError, ValueError):
            return True
        # GainThresholdEvent carries exactly one shard IntAttr with value 1.
        # Other event TAC fields can be supplied by callers through the same
        # transient map, keeping this evaluator independent of card names.
        event_tac = dict(ctx.event_tac or
                         (ctx.bstate or {}).get("event_tac") or {})
        color = (ctx.bstate or {}).get("gain_threshold_color")
        if color is not None:
            for name, flag in game_engine.SHARD_TO_FLAG.items():
                if int(flag) == int(color):
                    event_tac[_tac_attr_hash(name.title())] = 1
                    break

        def _matches(condition):
            if not isinstance(condition, dict):
                return True
            minimum_hash = _tac_attr_hash("MinimumValues")
            subset_hash = _tac_attr_hash("HasAsSubset")
            for key, value in (condition.get(minimum_hash) or {}).items():
                if int(event_tac.get(key, 0) or 0) < int(value or 0):
                    return False
            for key, value in (condition.get(subset_hash) or {}).items():
                if isinstance(value, dict):
                    if not _matches_nested(event_tac, key, value):
                        return False
                elif event_tac.get(key) != value:
                    return False
            return True

        def _matches_nested(actual, key, expected):
            # Nested event TACs are represented with the same hash-keyed
            # mapping.  This helper intentionally requires the expected
            # values rather than treating missing data as a wildcard.
            value = actual.get(key)
            if not isinstance(value, dict):
                return False
            return all(value.get(k) == v for k, v in expected.items())

        conditions_hash = _tac_attr_hash("Conditions")
        conditions = required.get(conditions_hash)
        if not conditions:
            conditions = [required]
        return all(_matches(condition) for condition in conditions)
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
            if pfilter in ("Self", "You", "Controller"):
                if ctx.bstate.get("pvp"):
                    if int(card.get("user_id", 0) or 0) != int(
                            ctx.ability_source_owner_id or 0):
                        continue
                elif side != src_side:
                    continue
            if pfilter in ("Opposing", "Opponents", "MultipleOpponents"):
                if ctx.bstate.get("pvp"):
                    if int(card.get("user_id", 0) or 0) == int(
                            ctx.ability_source_owner_id or 0):
                        continue
                elif side == src_side:
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
        try:
            rhs_value = int(rhs)
        except (TypeError, ValueError):
            # Both literal values and variable names are serialized as
            # strings.  CountListAttr variables (for example
            # ``SacrificedCards``) are populated by earlier effects.
            if rhs not in ctx.ability_variables:
                return False
            try:
                rhs_value = int(ctx.ability_variables[rhs])
            except (TypeError, ValueError):
                return False
        try:
            lhs_value = int(ctx.ability_variables[lhs])
        except (TypeError, ValueError):
            return False
        return _compare(lhs_value, op, rhs_value)

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
    """Evaluate an ability's condition trees from its current record.

    The argument is normally the typed ``AbilityTemplate`` mapping produced by
    Records.  A JSON string is accepted only for the leaf-condition adapter,
    not as a second rules-data source.
    """
    if not raw_json:
        return True
    if isinstance(raw_json, Mapping):
        rec = raw_json
    else:
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
