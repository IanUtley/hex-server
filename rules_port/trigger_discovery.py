"""RulesPort-owned trigger candidate discovery.

This module deliberately stops at the candidate boundary.  Records supply
ability lists and the session database supplies current card locations; the
legacy effect resolver is not involved in deciding which sources may react.
Condition evaluation and effect scheduling happen after this typed set has
been handed to the port trigger dispatcher.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TriggerCandidate:
    source_uid: int
    ability_guids: tuple[str, ...]


def ability_matches_keyword(ability_guid, keyword):
    """Match an authored keyword for ActivateTriggered natively."""
    from gamedata import DEFAULT_RECORD_STORE, ability_graph
    key = str(keyword or "").lower()
    if key.endswith("ies"):
        key = key[:-3] + "y"
    elif key.endswith("s"):
        key = key[:-1]
    graph = ability_graph(DEFAULT_RECORD_STORE, str(ability_guid).lower())
    if graph is None:
        return False
    if key == "momentum":
        return "CardInspiredEvent" in str(graph.trigger_event_type or "")
    if key == "deploy":
        # ActivateTriggered's m_Keyword "Deploy" manually triggers the card's
        # authored AsEntersPlay abilities (Daybloom, "Trigger the Deploy of
        # that troop").
        return "AsEntersPlayEvent" in str(graph.trigger_event_type or "")
    if key != "deathcry":
        return False
    try:
        from .tac import _tac_attr_hash, decode_tac
        serialized = graph.source.field("m_SerializedTAC")
        data = (serialized.field("data", "") if hasattr(serialized, "field")
                else serialized.get("data", "") if isinstance(serialized, dict)
                else "")
        return bool(data and _tac_attr_hash("Deathcry") in decode_tac(data))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False


class RecordsTriggerDiscovery:
    """Enumerate trigger sources from live state and Records metadata."""

    def __init__(self, db, handler, session, player_uid, ai_uid, battle_state):
        self.db = db
        self.handler = handler
        self.session = session
        self.player_uid = player_uid
        self.ai_uid = ai_uid
        self.battle_state = battle_state or {}

    def _opposing_owner(self, owner_id):
        if self.battle_state.get("pvp"):
            for pid in self.battle_state.get("pids") or ():
                if int(pid) != int(owner_id):
                    return int(pid)
            for pid in (self.battle_state.get("champ_map") or {}).keys():
                if int(pid) != int(owner_id):
                    return int(pid)
            return None
        profile = getattr(self.handler, "user_profile", None) or {}
        return 0 if int(owner_id or 0) else int(profile.get("id", 0) or 0)

    def _owns_champion(self, champion_uid):
        """Whether ``champion_uid`` is the champion this handler plays.

        A handler's runtime champion-power list belongs to its own champion.
        When the handler has not recorded one (test doubles, partial
        checkpoints) keep the historical behaviour of trusting the list.
        """
        scid = getattr(self.handler, "_player_champ_scid", None)
        if scid is None:
            return True
        try:
            raw = getattr(getattr(scid, "uid", scid), "uid64", scid)
            return int(raw) == int(champion_uid)
        except (TypeError, ValueError):
            return True

    def discover(self, event_type: str, source_uid=None,
                 source_owner_uid=None, extra_target=None, zones=None):
        """Return deterministic source/ability candidates for one event."""
        from gamedata import DEFAULT_RECORD_STORE, ability_graph
        from pvp_db import (db_card_ability_payload, db_card_owner_id,
                            db_cards_in_zones_with_abilities,
                            db_champion_trigger_ability_guids)

        def card_abilities(uid):
            payload = db_card_ability_payload(
                self.session.session_id, int(uid), conn=self.db)
            try:
                return [str(value).lower() for value in json.loads(payload or "[]")]
            except (TypeError, ValueError, json.JSONDecodeError):
                return []

        def zone_holders(owner, requested_zones):
            values = list(dict.fromkeys(
                list(requested_zones) +
                (["mod"] if "warzone" in requested_zones else [])))
            result = {}
            for uid, payload in db_cards_in_zones_with_abilities(
                    self.session.session_id, owner, values, conn=self.db):
                try:
                    abilities = [str(value).lower() for value in
                                 json.loads(payload or "[]")]
                except (TypeError, ValueError, json.JSONDecodeError):
                    abilities = []
                if abilities:
                    result[int(uid)] = abilities
            return result

        def champion_holders(owner):
            owner = int(owner or 0)
            # PvP owners are raw participant ids, which are also the
            # game_cards owner.  ``user_profile["id"]`` is the local database
            # id, so the PvE identity check below rejects every PvP
            # champion.  The client resolves champion triggers from the
            # active player's champion card (EndPhaseState.OnEntry), so
            # resolve the champion straight from the session for either
            # participant.
            if self.battle_state.get("pvp"):
                from pvp_db import db_card_basic
                champion_uid = int(
                    (self.battle_state.get("champ_map") or {}).get(
                        str(owner), 0) or 0)
                if not champion_uid:
                    return {}
                champion_basic = db_card_basic(
                    self.session.session_id, champion_uid, conn=self.db)
                if not champion_basic:
                    return {}
                guid = champion_basic[0]
                # Each participant's own handler carries only ITS champion's
                # configured powers, so the runtime list may describe the
                # local champion rather than the owner being resolved.  Apply
                # it only to the champion this handler owns; an opposing
                # champion's authored triggers come from its metadata.
                configured = (
                    getattr(self.handler, "_player_champ_abilities", [])
                    if self._owns_champion(champion_uid) else [])
            else:
                profile = getattr(self.handler, "user_profile", None) or {}
                player_id = int(profile.get("id", 0) or 0)
                # PvE uses owner 0 for the AI champion; it is a valid
                # controller identity, not an absent owner.
                if owner not in (0, player_id):
                    return {}
                guid = (getattr(self.handler, "_ai_champ_guid", None)
                        if owner == 0 else
                        getattr(self.handler, "_player_champ_guid", None))
                scid = (getattr(self.handler, "_ai_champ_scid", None)
                        if owner == 0 else
                        getattr(self.handler, "_player_champ_scid", None))
                if scid is None:
                    return {}
                champion_uid = int(scid.uid.uid64)
                configured = (getattr(self.handler, "_ai_champ_ability_guids", [])
                              if owner == 0 else
                              getattr(self.handler, "_player_champ_abilities", []))
            abilities = []
            if guid:
                abilities.extend(str(row[0]).lower() for row in
                                 db_champion_trigger_ability_guids(
                                     str(guid), conn=self.db))
            for value in configured or ():
                key = str(getattr(value, "guid", value)).lower()
                graph = ability_graph(DEFAULT_RECORD_STORE, key)
                if graph is not None and graph.trigger_event_type:
                    abilities.append(key)
            dynamic = getattr(self.handler,
                              "_champion_granted_ability_guids", {}) or {}
            from .effect_lifetimes import champion_grants
            dynamic_values: list[str] = list(
                dynamic.get(int(champion_uid), ()) or ())
            dynamic_values.extend(champion_grants(
                self.battle_state, int(champion_uid)))
            for value in dict.fromkeys(dynamic_values):
                key = str(value).lower()
                graph = ability_graph(DEFAULT_RECORD_STORE, key)
                if graph is None:
                    continue
                if graph.trigger_event_type:
                    if str(graph.trigger_event_type).endswith("GameStartedEvent"):
                        from .effect_lifetimes import gamestart_grant_resolved
                        if gamestart_grant_resolved(
                                self.battle_state, int(champion_uid), key):
                            # Already resolved when it was granted during setup.
                            continue
                    abilities.append(key)
                    continue
                # A granted permanent CardModifier is a continuous aura, not
                # an event trigger. Revisit it only after an actual zone
                # entry so a newly present card can receive the modifier.
                # It must not observe would-draw/would-enter events and
                # replace a draw with a chain item.
                if (event_type == "CardEnteredZoneEvent" and
                        any(effect.concrete_type ==
                            "CardModifierAbilityEffectTemplate" and
                            str(effect.duration).lower() == "permanent"
                            for effect in graph.effects) and
                        any(target.card_filter for target in graph.targets)):
                    abilities.append(key)
            return {champion_uid: list(dict.fromkeys(abilities))}

        event_type = str(event_type)

        def _uid_int(value):
            """Coerce a raw id, UID, or SessionCardId to its uint64 value."""
            if value is None:
                return None
            try:
                return int(getattr(value, "uid64", value))
            except (TypeError, ValueError):
                inner = getattr(value, "value", None)
                if inner is not None and inner is not value:
                    try:
                        return int(getattr(inner, "uid64", inner))
                    except (TypeError, ValueError):
                        return None
                return None

        source_uid = _uid_int(source_uid)
        extra_target = _uid_int(extra_target)
        candidates: dict[int, list[str]] = {}
        if source_uid is not None:
            uid = int(source_uid)
            candidates.setdefault(uid, []).extend(card_abilities(uid))
        # CardCreatedEvent is the synchronous client call to
        # ActivateCardCreationAbilities(newCard). It is scoped to that new
        # card's own trigger collection; unlike OtherCardCreatedEvent it is
        # not broadcast to every registered trigger listener.
        if event_type == "CardCreatedEvent":
            return self._freeze(candidates)
        # CardCastEvent's target is the card being cast, not another
        # registered trigger source. A troop that only gained Warzone trigger
        # registration as this cast resolved cannot hear its own earlier cast
        # event.
        if (extra_target is not None and
                int(extra_target) != int(source_uid or 0) and
                event_type != "CardCastEvent"):
            uid = int(extra_target)
            candidates.setdefault(uid, []).extend(card_abilities(uid))

        owner_id = source_owner_uid
        if owner_id is None and source_uid is not None:
            owner_id = db_card_owner_id(
                self.session.session_id, int(source_uid), conn=self.db)
            if owner_id is None:
                owner_id = 0

        if owner_id is None:
            return self._freeze(candidates)

        # C# keeps event-type registrations for cards as they move between
        # collections (Session.EventMaps.Current/Previous). The event's
        # ``zones`` payload describes the card involved in that event; it does
        # not describe where listener cards are registered. Scan every current
        # listener collection on both sides, then let the native dispatcher
        # apply each ability's TriggerCollectionFlags and authored conditions.
        # This includes triggers held in Void or Hand for start-turn, cast,
        # damage, and other events, and preserves the client behavior where an
        # opposing champion can observe an unrestricted event.
        other = self._opposing_owner(owner_id)
        sides = [int(owner_id)]
        if other is not None and int(other) not in sides:
            sides.append(int(other))
        zone_sets: list[tuple[str, ...]] = [(
            "warzone", "hand", "deck", "discard", "void",
            "underground", "CastSpells", "PlayedResources", "choosing",
            "mod", "simulacrum")]

        # Champions have no ``game_cards`` row, so scan both champion sources;
        # authored conditions decide which side actually responds.
        for side in sides:
            for uid, abilities in champion_holders(side).items():
                candidates.setdefault(int(uid), []).extend(abilities)
            for zone_group in zone_sets:
                for uid, abilities in zone_holders(side, zone_group).items():
                    if (event_type == "CardEnteredZoneEvent" or
                            (int(uid) != int(source_uid or 0) and
                             int(uid) != int(extra_target or 0))):
                        candidates.setdefault(int(uid), []).extend(abilities)
        return self._freeze(candidates)

    @staticmethod
    def _freeze(candidates: dict[int, list[str]]):
        # A card's ability fires at most once per event; the same card can be
        # reported by more than one zone or side scan, so drop repeats here.
        return tuple(
            TriggerCandidate(int(uid), tuple(dict.fromkeys(
                str(g).lower() for g in abilities)))
            for uid, abilities in candidates.items() if abilities)


__all__ = ["RecordsTriggerDiscovery", "TriggerCandidate"]
