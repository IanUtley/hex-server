"""Read-only bridge from the ported rules kernel to existing PVP state.

The engine never queries SQLite directly.  This adapter consumes the named
``pvp_db`` API and the already-persisted battle-state dictionary, keeping
``game_cards`` as the sole mutable card source while Records remain static
metadata authority.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib
import json
import sqlite3
from typing import Any, Callable, Mapping

import game_engine
from domain.enums import ECardAttributes, ECardTypes, card_type_from_db
from gamedata import DEFAULT_RECORD_STORE, ability_graph


_COLLECTION_BY_LOCATION = {
    "deck": game_engine.ECardCollections.Deck,
    "hand": game_engine.ECardCollections.Hand,
    "warzone": game_engine.ECardCollections.Warzone,
    "discard": game_engine.ECardCollections.Discard,
    "void": game_engine.ECardCollections.Void,
    "underground": game_engine.ECardCollections.Underground,
    "PlayedResources": game_engine.ECardCollections.PlayedResources,
    "CastSpells": game_engine.ECardCollections.CastSpells,
}

_LOCATION_BY_COLLECTION = {
    game_engine.ECardCollections.Deck: "deck",
    game_engine.ECardCollections.Hand: "hand",
    game_engine.ECardCollections.Warzone: "warzone",
    game_engine.ECardCollections.Discard: "discard",
    game_engine.ECardCollections.Void: "void",
    game_engine.ECardCollections.Underground: "underground",
    game_engine.ECardCollections.PlayedResources: "PlayedResources",
    game_engine.ECardCollections.CastSpells: "CastSpells",
}


def _raw_player_id(value) -> int:
    value = int(getattr(value, "uid64", value))
    # HConnect uses typed ServicePlayer UIDs; game_cards stores the raw owner.
    return value >> 8 if (value & 0xFF) == 244 else value


@dataclass(frozen=True)
class RuntimeCard:
    session_card_id: int
    template_guid: str
    owner_id: int
    location: str
    collection: int
    card_type: int
    state: int
    attributes: int
    casting_cost: int
    thresholds: tuple[dict[str, Any], ...]
    abilities: tuple[str, ...]

    def is_tapped(self) -> bool:
        return bool(self.state & game_engine.ECardStates.Tapped)

    def can_ready_at_start_of_turn(self) -> bool:
        # Restrictions such as Frozen are represented in the live state
        # adapter's policy hook; ordinary tapped cards can ready.
        return self.is_tapped()

    def has_type(self, card_type) -> bool:
        return bool(self.card_type & int(card_type))

    @property
    def in_warzone(self) -> bool:
        return self.location == "warzone"

    @property
    def is_troop(self) -> bool:
        return self.has_type(ECardTypes.Troop)

    def cares_about_combat_phase(self, phase) -> bool:
        """Mirror ``Card.CaresAboutCombatPhase`` for native combat facts."""
        first_strike = bool(self.attributes & int(
            game_engine.ECardAttributes.FirstStrike))
        dual_strike = bool(self.attributes & int(
            game_engine.ECardAttributes.DualStrike))
        if getattr(phase, "name", "") == "FIRST_STRIKE" or int(phase) == 2:
            return first_strike or dual_strike
        return not first_strike or dual_strike


@dataclass(frozen=True)
class RuntimePlayer:
    player_id: int
    current_resource_pool: int
    resource_thresholds: Mapping[Any, int]
    charge_points: int = 0
    spell_points: int = 0


class SQLiteCardMutationAdapter:
    """Map emitted card-zone mutations through the existing PVP DB facade.

    Rules and event construction remain SQL-free.  Unknown collection values
    are rejected instead of silently assigning a guessed location.
    """

    def __init__(self, session_id, *, pvp_api=None) -> None:
        self.session_id = int(session_id)
        self._pvp = pvp_api or importlib.import_module("pvp_db")

    @staticmethod
    def _card_uid(event) -> int | None:
        value = getattr(event, "session_card_id", None)
        value = getattr(value, "uid", value)
        try:
            return int(getattr(value, "uid64", value))
        except (TypeError, ValueError):
            return None

    def apply_card_moved(self, event) -> bool:
        card_uid = self._card_uid(event)
        location = _LOCATION_BY_COLLECTION.get(getattr(event, "collection", None))
        if card_uid is None or location is None:
            return False
        self._pvp.db_set_card_location(self.session_id, card_uid, location)
        return True

    __call__ = apply_card_moved


class PvpRuntimeFacts:
    """Facts required by C#-ported predicates, backed by existing APIs.

    ``play_validator`` and ``target_validator`` are injected domain adapters
    for the portions that require full modifier/target semantics. The built-in
    checks are deliberately conservative and never authorize a card outside
    its owner/zone/phase/resource/threshold constraints.
    """

    def __init__(self, session_id, battle_state: Mapping[str, Any], *,
                 player_uid, ai_uid, play_validator: Callable | None = None,
                 target_validator: Callable | None = None, pvp_api=None) -> None:
        self.session_id = session_id
        self.battle_state = battle_state
        self.player_uid = player_uid
        self.ai_uid = ai_uid
        self.play_validator = play_validator
        self.target_validator = target_validator
        # Importing pvp_db initializes the shared SQLite connection. Keep it
        # lazy so merely importing rules_port never waits on a live writer.
        self._pvp = pvp_api or importlib.import_module("pvp_db")

    def _location_row(self, card_id):
        card_id = int(getattr(card_id, "uid64", card_id))
        for location in _COLLECTION_BY_LOCATION:
            for row in self._pvp.db_game_cards_at_location(self.session_id, location):
                if int(row[0]) == card_id:
                    return location, row
        return None, None

    def _thresholds(self, template_guid) -> tuple[dict[str, Any], ...]:
        row = self._pvp.db_card_template_thresholds(template_guid)
        if not row or not row[0]:
            return ()
        try:
            values = json.loads(row[0])
        except (TypeError, ValueError, json.JSONDecodeError):
            return ()
        return tuple(item for item in values if isinstance(item, dict))

    def _effective_cost(self, card_uid, template_guid) -> int:
        """Resolve the payable cost through the shared PVP domain API.

        ``db_card_template_field(..., "cost")`` is the printed cost only.  A
        RulesPort legality check must agree with the payment path, including
        per-instance and continuous reductions.  Keep a small compatibility
        fallback for focused test doubles and older deployments that do not
        expose the new facade method yet.
        """
        resolver = getattr(self._pvp, "db_game_card_effective_cost", None)
        if callable(resolver):
            try:
                return max(0, int(resolver(
                    self.session_id, int(card_uid), self.battle_state)))
            except (TypeError, ValueError, sqlite3.Error):
                if self.battle_state.get("_rules_port_attached"):
                    raise
                # Focused test doubles and older non-battle callers may not
                # expose the effective-cost facade yet.
                pass
        if self.battle_state.get("_rules_port_attached"):
            raise RuntimeError(
                "RulesPort runtime facts require the effective-cost facade")
        cost = self._pvp.db_card_template_field(template_guid, "cost") or 0
        return max(0, int(cost or 0))

    def _effective_attributes(self, card_uid, stored) -> int:
        """Resolve the attribute bits every combat keyword predicate reads.

        ``RuntimeCard.attributes`` is only ever tested for combat keywords
        (Speed, CantAttack, Defensive, FirstStrike, CantBlock), so it must be
        the same effective projection that builds the attack/block option
        lists and applies temporary grants.  Reading the instance column alone
        rejected a troop that surfaced this turn — it carries Speed as a
        temporary attribute — after the client had been offered the attack.
        """
        resolver = getattr(self._pvp, "db_game_card_effective_attributes", None)
        if callable(resolver):
            return int(resolver(self.session_id, int(card_uid),
                                self.battle_state) or 0)
        if self.battle_state.get("_rules_port_attached"):
            raise RuntimeError(
                "RulesPort runtime facts require the effective-attribute facade")
        # Focused test doubles and older non-battle callers project the
        # instance column until they expose the facade.
        return int(stored or 0)

    def get_card(self, card_id) -> RuntimeCard | None:
        location, row = self._location_row(card_id)
        if row is None:
            return None
        card_uid, template_guid, owner, type_name, state, abilities, attributes = row
        cost = self._effective_cost(card_uid, template_guid)
        attributes = self._effective_attributes(card_uid, attributes)
        try:
            ability_ids = tuple(json.loads(abilities or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            ability_ids = tuple(self._pvp.db_card_ability_list(self.session_id, card_uid))
        return RuntimeCard(
            int(card_uid), str(template_guid), int(owner), location,
            _COLLECTION_BY_LOCATION[location], card_type_from_db(type_name),
            int(state or 0), int(attributes or 0), int(cost or 0),
            self._thresholds(template_guid), tuple(str(value).lower()
                                                   for value in ability_ids),
        )

    def get_player(self, player_id) -> RuntimePlayer:
        raw = _raw_player_id(player_id)
        player_raw, ai_raw = _raw_player_id(self.player_uid), _raw_player_id(self.ai_uid)
        if self.battle_state.get("pvp"):
            return RuntimePlayer(
                raw,
                int(self.battle_state.get(f"res_{raw}", 0) or 0),
                self.battle_state.get(f"thresh_{raw}", {}) or {},
                int(self.battle_state.get(f"chg_{raw}", 0) or 0),
                int(self.battle_state.get(f"sp_{raw}", 0) or 0),
            )
        prefix = "player" if raw == player_raw else "ai" if raw == ai_raw else ""
        state = self.battle_state
        return RuntimePlayer(
            raw, int(state.get(f"{prefix}_resources", 0)),
            state.get(f"{prefix}_threshold", {}) if prefix else {},
            int(state.get(f"{prefix}_charges", 0)),
            int(state.get(f"{prefix}_spell_points", 0)),
        )

    def _has_thresholds(self, card: RuntimeCard, player: RuntimePlayer) -> bool:
        for requirement in card.thresholds:
            color = requirement.get("color", requirement.get("color_flags"))
            amount = requirement.get("quantity", requirement.get(
                "amount", requirement.get("threshold_color_requirement", 0)))
            try:
                if int(player.resource_thresholds.get(color, 0)) < int(amount):
                    return False
            except (TypeError, ValueError):
                return False
        return True

    def can_play_card(self, card: RuntimeCard, player_id, playing_for_free=False) -> bool:
        if self.play_validator is not None:
            return bool(self.play_validator(card, player_id, playing_for_free))
        player = self.get_player(player_id)
        request_uid = getattr(self, "client_player_uid", self.player_uid)
        if self.battle_state.get("pvp"):
            owner_id = _raw_player_id(player_id)
        else:
            owner_id = getattr(self, "player_owner_id", None)
            if _raw_player_id(player_id) != _raw_player_id(request_uid):
                owner_id = getattr(self, "ai_owner_id", None)
        if owner_id is None:
            owner_id = player.player_id
        if card.owner_id != int(owner_id) or card.location != "hand":
            return False
        if not self._has_thresholds(card, player):
            return False
        return bool(playing_for_free or player.current_resource_pool >= card.casting_cost)

    def can_activate_ability(self, card: RuntimeCard, player_id, ability_template_id) -> bool:
        # ``game_cards.owner_user_id`` is the profile/reckoning id, while the
        # transaction carries the typed GamePlayer/ServicePlayer UID.  The
        # live HConnect adapter supplies the profile ids when those namespaces
        # differ; fall back to the raw UID for standalone kernel tests.
        request_uid = getattr(self, "client_player_uid", self.player_uid)
        if self.battle_state.get("pvp"):
            owner_id = _raw_player_id(player_id)
        else:
            owner_id = getattr(self, "player_owner_id", None)
            if _raw_player_id(player_id) != _raw_player_id(request_uid):
                owner_id = getattr(self, "ai_owner_id", None)
        # Some Practice sessions do not expose a profile id on attach.  The
        # card row's user_id is still authoritative for this already-resolved
        # source card; do not misclassify the human as an unrelated raw UID
        # and fall back to legacy activation.
        if owner_id is None:
            owner_id = card.owner_id
        if (card.owner_id != int(owner_id) or
                str(ability_template_id).lower() not in card.abilities):
            return False
        # Manual abilities still honor the client's authored
        # ``m_TriggerCollectionFlags`` gate.  In particular, Grave Nibbler's
        # Tunnel ability is Hand-only and must not become available after the
        # troop has entered Warzone.
        graph = ability_graph(DEFAULT_RECORD_STORE,
                              str(ability_template_id).lower())
        flags = str(getattr(graph, "trigger_collection_flags", "") or "")
        if flags and str(card.location or "").lower() not in {
                zone.strip().lower() for zone in flags.split("|") if zone.strip()}:
            return False
        validator = getattr(self, "ability_validator", None)
        return (bool(validator(card, player_id, ability_template_id))
                if callable(validator) else True)

    def can_activate_champion_ability(self, source_card_id, player_id,
                                      ability_template_id) -> bool:
        """Validate a charge power whose champion card is synthetic.

        Champion SessionCardIds are emitted by the protocol but intentionally
        do not live in ``game_cards``.  The HConnect attach seam supplies the
        concrete player/AI champion IDs; ability existence and authored costs
        remain Records/SQLite metadata checks.
        """
        try:
            source_id = int(getattr(source_card_id, "uid64", source_card_id))
        except (TypeError, ValueError):
            return False
        request_raw = _raw_player_id(player_id)
        owner_raw = _raw_player_id(getattr(self, "client_player_uid", self.player_uid))
        expected = getattr(self, "player_champion_card_id", None)
        if request_raw != owner_raw:
            expected = getattr(self, "ai_champion_card_id", None)
        try:
            expected = getattr(expected, "uid", expected)
            expected = int(getattr(expected, "uid64", expected))
        except (TypeError, ValueError):
            expected = 0
        # Practice/reconnected sessions can omit the handler champion fields,
        # while battle state still carries the authoritative champion map.
        # Accept the matching mapped SessionCardId rather than rejecting the
        # synthetic champion source as an ordinary missing game_cards row.
        valid_ids = {expected} if expected else set()
        for value in (getattr(self, "battle_state", {}) or {}).get(
                "champ_map", {}).values():
            try:
                value = getattr(value, "uid", value)
                valid_ids.add(int(getattr(value, "uid64", value)))
            except (TypeError, ValueError):
                continue
        if source_id not in valid_ids:
            return False
        try:
            guid = str(ability_template_id).lower()
            # A campaign champion's charge power may be supplied by a
            # selected champion talent.  It is still serialized on the
            # synthetic champion card, but its authored cost lives in
            # talent_abilities rather than champion_abilities.
            attached = getattr(self, "player_champion_ability_guids", None)
            if request_raw != owner_raw:
                attached = getattr(self, "ai_champion_ability_guids", None)
            if attached is not None and guid not in {
                    str(value).lower() for value in attached}:
                return False
            if self.champion_ability_uses_exhausted(
                    source_id, ability_graph(DEFAULT_RECORD_STORE, guid)):
                return False
            from pvp_db import db_champion_ability_costs, db_charge_ability_cost
            return (db_champion_ability_costs(guid) is not None or
                    db_charge_ability_cost(guid) is not None)
        except Exception:
            return False

    # ── champion power usage (m_UsesPerGame / ONE-SHOT) ────────────────────
    #
    # A champion power belongs to a synthetic SessionCardId with no
    # ``game_cards`` row, so ``card_uses`` cannot hold its per-game count the
    # way an ordinary card ability does.  Keep it in the shared battle state
    # next to the champion counters, so Practice/PvE and tournament PvP gate
    # and spend a ONE-SHOT by the same rule.

    def _champion_power_key(self, source_card_id, graph):
        """Return the usage key for a champion power, else None."""
        guid = str(getattr(graph, "guid", "") or "").lower()
        if not guid:
            return None
        try:
            source = int(getattr(source_card_id, "uid64", source_card_id))
        except (TypeError, ValueError):
            return None
        from .runtime_helpers import champion_uids_by_owner
        champions = champion_uids_by_owner(self, self.battle_state)
        if source not in set(champions.values()):
            return None  # an ordinary card keeps its own card_uses record
        return guid

    def champion_ability_uses_exhausted(self, source_card_id, graph) -> bool:
        """Whether an authored ``m_UsesPerGame`` champion power is spent."""
        key = self._champion_power_key(source_card_id, graph)
        if key is None:
            return False
        limit = int(getattr(getattr(graph, "costs", None),
                            "uses_per_game", 0) or 0)
        if limit <= 0:
            return False
        used = int((self.battle_state.get("champion_ability_uses") or {}).get(
            key, 0) or 0)
        return used >= limit

    def consume_champion_ability_use(self, source_card_id, graph) -> int:
        """Record one activation of an authored (possibly limited) power."""
        key = self._champion_power_key(source_card_id, graph)
        if key is None:
            return 0
        uses = self.battle_state.setdefault("champion_ability_uses", {})
        uses[key] = int(uses.get(key, 0) or 0) + 1
        return uses[key]

    def can_pay_ability_cost(self, ability) -> bool:
        """Check authored activation costs before an ability enters the chain.

        Keep this check identical to the payment decision made when the
        ability is pushed.  Previously this method had a second hand-written
        implementation, which omitted spell-power escalation and could select
        the AI pool when metadata ownership was unset.
        """
        from .costs import plan_ability_cost
        metadata = getattr(ability, "metadata", ability)
        costs = getattr(metadata, "costs", None)
        if costs is None:
            return True
        owner = getattr(metadata, "owner_id", None)
        if not owner:
            source_uid = getattr(metadata, "source_uid", None)
            source = self.get_card(source_uid) if source_uid is not None else None
            owner = getattr(source, "owner_id", None)
        if self.battle_state.get("pvp") and owner is not None:
            # PvP has two human participants and no stable player/AI side.
            # The ability metadata carries the raw participant id, so never
            # use the handler that originally attached the cached facts to
            # select the resource pool.
            owner = int(owner)
            player = self.get_player(owner)
            plan = plan_ability_cost(
                costs, getattr(metadata, "activation", None),
                current_resource=player.current_resource_pool,
                charges=player.charge_points,
                spell_points=player.spell_points,
                health=int(self.battle_state.get(f"hp_{owner}", 25) or 0),
                spell_uses=self.battle_state.get(f"sp_uses_{owner}", {}) or {},
                ability_key=str(getattr(metadata, "ability_template_id", "")),
            )
            return plan is not None
        player_owner = getattr(self, "player_owner_id", None)
        is_player = (owner is not None and
                     (int(owner) == int(player_owner or -1) or
                      int(owner) == _raw_player_id(self.player_uid)))
        player = self.get_player(self.player_uid if is_player else self.ai_uid)
        prefix = "player" if is_player else "ai"
        plan = plan_ability_cost(
            costs, getattr(metadata, "activation", None),
            current_resource=player.current_resource_pool,
            charges=player.charge_points,
            spell_points=player.spell_points,
            health=int(self.battle_state.get(f"{prefix}_health", 25) or 0),
            spell_uses=self.battle_state.get(f"{prefix}_sp_uses", {}) or {},
            ability_key=str(getattr(metadata, "ability_template_id", "")),
        )
        return plan is not None

    def _resolved_owner_id(self, player_id) -> int:
        """Map a transaction participant onto the ``game_cards.user_id`` domain.

        ``game_cards`` stores the profile/reckoning id for the human (and 0
        for the Practice AI) while the transaction carries the typed
        ServicePlayer UID, so a raw ``_raw_player_id`` comparison rejects the
        owner's own troops.  The attach seam supplies both ids; fall back to
        the raw UID for standalone kernel tests.
        """
        if self.battle_state.get("pvp"):
            return _raw_player_id(player_id)
        request_uid = getattr(self, "client_player_uid", self.player_uid)
        owner_id = getattr(self, "player_owner_id", None)
        if _raw_player_id(player_id) != _raw_player_id(request_uid):
            owner_id = getattr(self, "ai_owner_id", None)
        if owner_id is None:
            owner_id = _raw_player_id(player_id)
        return int(owner_id)

    def can_attack(self, attacker: RuntimeCard, defender: RuntimeCard, player_id) -> bool:
        from .combat_rules import card_int_attr
        if not (attacker.owner_id == self._resolved_owner_id(player_id) and
                attacker.in_warzone and attacker.is_troop and
                not attacker.is_tapped() and
                # Summoned troops carry CameOutThisTurn until the next turn;
                # this is the client's summoning-sickness gate.
                (not bool(attacker.state & game_engine.ECardStates.CameOutThisTurn)
                 or bool(attacker.attributes & game_engine.ECardAttributes.Speed)) and
                not bool(attacker.attributes & ECardAttributes.CantAttack) and
                # Champion SessionCardIds are synthetic (not game_cards
                # rows), so ``defender`` is None for the normal face.
                (defender is None or defender.owner_id != attacker.owner_id)):
            return False
        # CardCounterTemplate "Stealth": while the defending champion holds a
        # stealth counter, opposing troops can't attack (client built-in
        # StealthCantAttackAbilityTemplateId).
        from .stealth import defending_champion_is_stealthed
        if defending_champion_is_stealthed(self, attacker, defender):
            return False
        # C# Card.CanAttack: a Defensive troop cannot attack unless it carries
        # the IgnoresDefensive int-attribute.
        if (bool(attacker.attributes & ECardAttributes.Defensive) and
                card_int_attr(None, self.session_id,
                              attacker.session_card_id,
                              "IgnoresDefensive") <= 0):
            return False
        return True

    def validate_blocks(self, session, declarations, player_id) -> bool:
        """Port of ``Session.AreDefenseDeclarationsLegal``.

        Each declared pair must satisfy the full ``Card.CanBlock(attacker)``
        predicate (Flight/SkyGuard, block restrictions and immunities) and the
        Feral "two or more troops" restriction.  The previous conservative
        check accepted flying/restricted blocks.
        """
        from .combat_rules import can_block, card_int_attr
        state = self.battle_state or {}
        seen: set[int] = set()
        declared: dict[int, int] = {}
        for attacker_id, blocker_ids in declarations:
            attacker = session.get_card(attacker_id)
            if attacker is None or not session.combat_manager.combats_with_attacker(attacker):
                return False
            try:
                attacker_key = int(getattr(attacker_id, "uid64", attacker_id))
            except (TypeError, ValueError):
                return False
            blockers = list(blocker_ids)
            declared[attacker_key] = declared.get(attacker_key, 0) + len(blockers)
            for blocker_id in blockers:
                try:
                    blocker_key = int(getattr(blocker_id, "uid64", blocker_id))
                except (TypeError, ValueError):
                    return False
                blocker = session.get_card(blocker_id)
                if (blocker is None or blocker_key in seen or
                        blocker.owner_id == attacker.owner_id or
                        not blocker.in_warzone or not blocker.is_troop or
                        blocker.is_tapped() or
                        bool(blocker.attributes & ECardAttributes.CantBlock)):
                    return False
                if not can_block(None, self.session_id, state,
                                 attacker_key, blocker_key):
                    return False
                seen.add(blocker_key)
        # C# Feral (IntAttrs): an attacker can only be blocked by 2+ troops.
        for attacker_key, count in declared.items():
            if 0 < count < 2 and card_int_attr(
                    None, self.session_id, attacker_key, "Feral") > 0:
                return False
        return True

    def validate_x_cost(self, player_id, activation_data) -> bool:
        try:
            return self.get_player(player_id).current_resource_pool >= int(
                activation_data.get("x_cost", 0) or 0)
        except (AttributeError, TypeError, ValueError):
            return False

    def validate_ability_targets(self, ability, activation_data, player_id) -> bool:
        return bool(self.target_validator and self.target_validator(
            ability, activation_data, player_id))


def attach_pvp_runtime_facts(port_session, game_session, battle_state, *,
                             player_uid, ai_uid, **validators) -> PvpRuntimeFacts:
    """Attach one opt-in facts bridge to an existing ported session."""
    facts = PvpRuntimeFacts(game_session.session_id, battle_state,
                            player_uid=player_uid, ai_uid=ai_uid, **validators)
    port_session.set_runtime_facts(facts)
    return facts
