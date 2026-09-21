"""Runtime facts exposed to native RulesPort condition leaves."""

from __future__ import annotations

import json

from .targeting import _shards


class ConditionContext:
    def __init__(self, db, session, bstate, event_type=None,
                 ability_source_uid=None, ability_source_owner_id=None,
                 trigger_uid=None, pl_t=None, ai_t=None, extra_target=None,
                 champions=None, ability_source_card_owner=None,
                 trigger_owner_id=None, event_source_collection=None,
                 event_destination_collection=None, event_previous_state=None,
                 uses_previous_state=False, event_int_attribute=None,
                 event_tac=None, event_previous_owner_id=None):
        self.db, self.session = db, session
        self.bstate = bstate or {}
        self.event_type = event_type
        self.ability_source_uid = ability_source_uid
        self.ability_source_owner_id = ability_source_owner_id
        self.trigger_owner_id = trigger_owner_id
        self.trigger_uid, self.extra_target = trigger_uid, extra_target
        self.pl_t, self.ai_t = pl_t, ai_t
        self.event_source_collection = event_source_collection
        self.event_destination_collection = event_destination_collection
        self.event_previous_state = event_previous_state
        # The controller the entering card had before the zone move.  The
        # client's ``TriggerCardEnteredZone`` requires both the current and the
        # previous controller for a "friendly zone" condition, so a card moved
        # out of an opponent's zone cannot satisfy ``m_Your``.
        self.event_previous_owner_id = event_previous_owner_id
        self.event_int_attribute = event_int_attribute
        self.event_tac = event_tac or {}
        self.uses_previous_state = bool(uses_previous_state)
        self.champions = champions or []
        # C# evaluates an effect condition against the ability instance, so
        # the context reads that instance's variables (RandomizeVariable and
        # friends) and its applied-effect bookkeeping.  The port keeps both in
        # the shared battle state; callers may still override them directly.
        self.ability_variables = dict(
            (self.bstate or {}).get("ability_variables") or {})
        self.applied_effects = dict(
            (self.bstate or {}).get("applied_effects") or {})
        self._cards = {}
        self._champions = {int(uid): (owner, name, health)
                           for uid, owner, name, health in self.champions}

    def _counter_counts(self, uid):
        from pvp_db import db_card_permanent_buffs
        try:
            data = json.loads(db_card_permanent_buffs(
                self.session.session_id, int(uid), conn=self.db) or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        counters = data.get("counters")
        guids = data.get("counter_guids")
        return (counters if isinstance(counters, dict) else {},
                guids if isinstance(guids, dict) else {})

    def card(self, uid):
        if uid is None:
            return None
        uid = int(uid)
        if uid in self._champions:
            owner, name, health = self._champions[uid]
            # PvP ownership is the raw participant id, not the compatibility
            # ``user_profile["id"]`` that ``handler._champion_targets`` reports.
            # C# resolves ``TriggerPlayerControlsAbilitySource`` from the
            # champion card's controller (EndPhaseState uses the active
            # player's champion), so map the champion back to its participant
            # here; otherwise the owner never matches and the trigger is
            # silently dropped.
            if self.bstate.get("pvp"):
                for pid, champ_uid in (
                        self.bstate.get("champ_map") or {}).items():
                    try:
                        if int(champ_uid) == uid:
                            owner = int(pid)
                            break
                    except (TypeError, ValueError):
                        continue
            counters, guids = self._counter_counts(uid)
            return {"card_uid": uid, "card_type": "Champion",
                    "location": "champions", "user_id": owner,
                    "owner_id": owner, "controller_id": owner,
                    "attack": 0, "defense": int(health or 0),
                    "name": name or "Champion", "cost": 0,
                    "shards": [], "attributes": 0,
                    "counters": counters, "counter_guids": guids}
        if uid in self._cards:
            return self._cards[uid]
        from pvp_db import db_condition_card_row
        row = db_condition_card_row(self.session.session_id, uid, conn=self.db)
        if not row:
            self._cards[uid] = None
            return None
        try:
            buffs = json.loads(row[16] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            buffs = {}
        if not isinstance(buffs, dict):
            buffs = {}
        counters, guids = self._counter_counts(uid)
        card = {"card_uid": int(row[0]), "card_type": row[1] or "",
                "location": row[2] or "", "user_id": row[3],
                "owner_id": row[3], "controller_id": row[3],
                "state": int(row[4] or 0),
                "attack": int(row[5] or 0) + int(row[14] or 0),
                "defense": int(row[6] or 0) + int(row[15] or 0),
                "template_guid": row[7] or "", "name": row[8] or "",
                "cost": int(row[9] or 0), "subtype": row[10] or "",
                "shards": _shards(row[11]),
                "attributes": int(row[12] or 0) | int(row[13] or 0),
                "int_attrs": buffs.get("int_attrs", {}),
                "counters": counters, "counter_guids": guids,
                "damaged_opponent_this_turn": list(
                    self.bstate.get("damaged_opponent_this_turn") or [])}
        self._cards[uid] = card
        return card

    def _zones(self, flags):
        mapping = {"Warzone": "warzone", "Hand": "hand", "Deck": "deck",
                   "Crypt": "discard", "Discard": "discard", "Void": "void",
                   "CastSpells": "CastSpells", "PlayedResources": "PlayedResources",
                   "Choosing": "choosing", "Underground": "underground",
                   "Champions": "champions"}
        return {mapping.get(value, value.lower()) for value in
                str(flags or "").split("|")
                if value and value.lower() not in {"none", "null"}}

    def _cards_in_zones(self, zones, user_id=None):
        from pvp_db import db_condition_cards_in_zones
        rows = db_condition_cards_in_zones(
            self.session.session_id, zones, user_id=user_id, conn=self.db)
        cards = [self.card(row[0]) for row in rows]
        cards = [card for card in cards if card is not None]
        if "champions" in zones:
            for uid, owner, name, health in self.champions:
                if user_id is None or int(owner or 0) == int(user_id):
                    card = self.card(uid)
                    if card is not None:
                        cards.append(card)
        return cards

    def _counter_count(self, card, counter_guid):
        if not card:
            return 0
        wanted = str(counter_guid or "").lower()
        total = 0
        for name, count in (card.get("counters") or {}).items():
            if str(name).lower() == wanted or str(
                    (card.get("counter_guids") or {}).get(name, "")).lower() == wanted:
                try:
                    total += int(count or 0)
                except (TypeError, ValueError):
                    pass
        return total
