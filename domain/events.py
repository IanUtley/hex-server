"""SessionEventArgs event classes extracted from game_engine.py."""

from typing import List, Dict

from domain.types import UID, ResourceId, SessionCardId, CombatId
from domain.enums import (
    ETurnPhases, ECardTypes, ECardCollections, ECardLocations, ECardStates,
    EAICardStates, ECardAttributes, ECardShards, EGemTypesNew, ECombatPhase,
    EPriorityContext, ECardUsage, ECardShard,
)
from domain.serializer import Serializer


class SessionEventArgs:
    CLASS_ID = 0

    def __init__(self):
        self.session_id = UID.invalid()
        self.ser = Serializer()

    def begin_write(self):
        self.ser.begin_write()
        self.ser.add_int(self.CLASS_ID)
        self.ser.add_uid(self.session_id)

    def end_write(self) -> bytes:
        return self.ser.end_write()

    def to_byte_array(self) -> bytes:
        raise NotImplementedError


class GameStartedSessionEventArgs(SessionEventArgs):
    CLASS_ID = 1

    def __init__(self):
        super().__init__()
        self.turn_order: List[UID] = []
        self.champion_names: List[str] = []
        self.champion_template_ids: List[ResourceId] = []
        self.sleeve_template_ids: List[ResourceId] = []
        self.board_template_ids: List[ResourceId] = []
        self.coin_template_ids: List[ResourceId] = []
        self.divisions: List[int] = []
        self.seed_w: int = 0
        self.seed_z: int = 0
        self.series_format = 0
        self.series_max_games = 0
        self.series_prev_winners: List[int] = []

    def to_byte_array(self) -> bytes:
        self.begin_write()
        self.ser.add_list_uid(self.turn_order)
        self.ser.add_list_string(self.champion_names)
        self.ser.add_list_resource_id(self.champion_template_ids)
        self.ser.add_list_resource_id(self.sleeve_template_ids)
        self.ser.add_list_resource_id(self.board_template_ids)
        self.ser.add_list_resource_id(self.coin_template_ids)
        self.ser.add_list_int(self.divisions)
        self.ser.add_ulong(self.seed_w)
        self.ser.add_ulong(self.seed_z)
        self.ser.add_enum_int(self.series_format)
        self.ser.add_int(self.series_max_games)
        self.ser.add_list_int(self.series_prev_winners)
        return self.end_write()


class FirstPlayerDictatedSessionEventArgs(SessionEventArgs):
    """Game.Shared.FirstPlayerDictatedSessionEventArgs (class 60): tells the
    clients which player won the coin flip.  Receiving it sets
    UIBattle.m_CoinFlipSkip so the coin-flip animation completes and the
    client processes the following phases (Mulligan dialog etc.)."""
    CLASS_ID = 60

    def __init__(self):
        super().__init__()
        self.player_id: UID = UID.invalid()

    def to_byte_array(self) -> bytes:
        self.begin_write()
        self.ser.add_uid(self.player_id)
        return self.end_write()


class GameEndedSessionEventArgs(SessionEventArgs):
    CLASS_ID = 2

    def __init__(self):
        super().__init__()
        self.winners: List[UID] = []
        self.losers: List[UID] = []

    def to_byte_array(self) -> bytes:
        self.begin_write()
        self.ser.add_list_uid(self.winners)
        self.ser.add_list_uid(self.losers)
        return self.end_write()


class TurnPhaseUpdatedSessionEventArgs(SessionEventArgs):
    CLASS_ID = 3

    def __init__(self):
        super().__init__()
        self.turn_phase = ETurnPhases.NotPlaying
        self.active_player_id = UID.invalid()
        self.priority_player_id = UID.invalid()
        self.priority_timer_elapsed = 0

    def to_byte_array(self) -> bytes:
        self.begin_write()
        self.ser.add_enum_int(self.turn_phase)
        self.ser.add_uid(self.active_player_id)
        self.ser.add_uid(self.priority_player_id)
        self.ser.add_long(self.priority_timer_elapsed)
        return self.end_write()


class PlayerWishesToDrawFirstSessionEventArgs(SessionEventArgs):
    CLASS_ID = 58

    def __init__(self):
        super().__init__()
        self.player_id = UID.invalid()

    def to_byte_array(self) -> bytes:
        self.begin_write()
        self.ser.add_uid(self.player_id)
        return self.end_write()


class PlayerWishesToPlayFirstSessionEventArgs(SessionEventArgs):
    CLASS_ID = 59

    def __init__(self):
        super().__init__()
        self.player_id = UID.invalid()

    def to_byte_array(self) -> bytes:
        self.begin_write()
        self.ser.add_uid(self.player_id)
        return self.end_write()


class PlayerMulliganedHandSessionEventArgs(SessionEventArgs):
    CLASS_ID = 5

    def __init__(self):
        super().__init__()
        self.player_id = UID.invalid()
        self.new_card_count = 0

    def to_byte_array(self) -> bytes:
        self.begin_write()
        s = self.ser
        s.add_uid(self.player_id)
        s.add_int(self.new_card_count)
        return self.end_write()


class PlayerAcceptedStartingHandSessionEventArgs(SessionEventArgs):
    CLASS_ID = 6

    def __init__(self):
        super().__init__()
        self.player_id = UID.invalid()
        self.mulliganed = False

    def to_byte_array(self) -> bytes:
        self.begin_write()
        s = self.ser
        s.add_uid(self.player_id)
        s.add_bool(self.mulliganed)
        return self.end_write()


class CardDrawnSessionEventArgs(SessionEventArgs):
    CLASS_ID = 7

    def __init__(self):
        super().__init__()
        self.player_id = UID.invalid()
        self.session_card_id = SessionCardId()
        self.nth_card_drawn = 0

    def to_byte_array(self) -> bytes:
        self.begin_write()
        s = self.ser
        s.add_uid(self.player_id)
        s.add_scid(self.session_card_id)
        s.add_int(self.nth_card_drawn)
        return self.end_write()


class CardDiscardedSessionEventArgs(SessionEventArgs):
    """A card was discarded from hand (class 10)."""
    CLASS_ID = 10

    def __init__(self):
        super().__init__()
        self.player_id = UID.invalid()
        self.session_card_id = SessionCardId()

    def to_byte_array(self) -> bytes:
        self.begin_write()
        s = self.ser
        s.add_uid(self.player_id)
        s.add_scid(self.session_card_id)
        return self.end_write()


class ChampionCardPlayedSessionEventArgs(SessionEventArgs):
    CLASS_ID = 14

    def __init__(self):
        super().__init__()
        self.player_id = UID.invalid()
        self.is_ai = False
        self.player_name = ""
        self.session_card_id = SessionCardId()

    def to_byte_array(self) -> bytes:
        self.begin_write()
        s = self.ser
        s.add_uid(self.player_id)
        s.add_bool(self.is_ai)
        s.add_string(self.player_name)
        s.add_scid(self.session_card_id)
        return self.end_write()


class TroopCardPlayedSessionEventArgs(SessionEventArgs):
    CLASS_ID = 15

    def __init__(self):
        super().__init__()
        self.player_id = UID.invalid()
        self.session_card_id = SessionCardId()

    def to_byte_array(self) -> bytes:
        self.begin_write()
        s = self.ser
        s.add_uid(self.player_id)
        s.add_scid(self.session_card_id)
        return self.end_write()


class SpellCardPlayedSessionEventArgs(SessionEventArgs):
    CLASS_ID = 17

    def __init__(self):
        super().__init__()
        self.player_id = UID.invalid()
        self.session_card_id = SessionCardId()

    def to_byte_array(self) -> bytes:
        self.begin_write()
        s = self.ser
        s.add_uid(self.player_id)
        s.add_scid(self.session_card_id)
        return self.end_write()


class ResourceCardPlayedSessionEventArgs(SessionEventArgs):
    CLASS_ID = 16

    def __init__(self):
        super().__init__()
        self.player_id = UID.invalid()
        self.session_card_id = SessionCardId()
        self.free = False

    def to_byte_array(self) -> bytes:
        self.begin_write()
        s = self.ser
        s.add_uid(self.player_id)
        s.add_scid(self.session_card_id)
        s.add_bool(self.free)
        return self.end_write()


class AbilityPushedOnChainSessionEventArgs(SessionEventArgs):
    """An ability/trigger was pushed onto the chain (class 22).

    The client renders it on the chain (BattleAnimationAddToChain) and uses the
    same event for the chain's priority/response windows. Mirrors the client's
    AbilityPushedOnChainSessionEventArgs field order exactly.
    """
    CLASS_ID = 22

    def __init__(self):
        super().__init__()
        self.ability_instance_id = 0
        self.source_card_id = SessionCardId()
        self.target_card_ids: List[SessionCardId] = []
        self.charge_point_cost = 0
        self.life_cost = 0
        self.resource_cost = 0
        self.free = False
        self.ignores_chain = False
        self.secondary_ability = False
        self.ability_template_id = ResourceId.invalid()
        self.ability_index = -1

    def to_byte_array(self) -> bytes:
        self.begin_write()
        s = self.ser
        s.add_long(self.ability_instance_id)
        s.add_scid(self.source_card_id)
        s.add_list_scid(self.target_card_ids)
        s.add_int(self.charge_point_cost)
        s.add_int(self.life_cost)
        s.add_int(self.resource_cost)
        s.add_bool(self.free)
        s.add_bool(self.ignores_chain)
        s.add_bool(self.secondary_ability)
        s.add_resource_id(self.ability_template_id)
        s.add_int(self.ability_index)
        return self.end_write()


class AbilityActivationDataRequiredSessionEventArgs(SessionEventArgs):
    """Server asks the player to supply activation data (targets) for a specific
    effect group while an ability resolves (class 23). The client shows the
    BattleStateUseTriggeredAbility -> BattleStateConfigureAbility picker for the
    effect instances, then sends the chosen data back. Used to prompt a
    choose-and-discard (e.g. Soothsaying) at the right point in the BOM."""
    CLASS_ID = 23

    def __init__(self):
        super().__init__()
        self.player_id = UID.invalid()
        self.ability_instance_id = 0
        self.ability_parent_id = 0
        self.source_card_id = SessionCardId()
        self.ability_template_id = ResourceId.invalid()
        self.effect_group_id = 0
        self.effect_instance_ids: List[int] = []
        self.resolve_chain = False

    def to_byte_array(self) -> bytes:
        self.begin_write()
        s = self.ser
        s.add_uid(self.player_id)
        s.add_long(self.ability_instance_id)
        s.add_long(self.ability_parent_id)
        s.add_scid(self.source_card_id)
        s.add_resource_id(self.ability_template_id)
        s.add_int(self.effect_group_id)
        s.add_list_int(self.effect_instance_ids)
        s.add_bool(self.resolve_chain)
        return self.end_write()


class TriggeredAbilityActivationDataRequiredSessionEventArgs(SessionEventArgs):
    """Class 39 — server asks the player to supply activation data for one or
    more TRIGGERED abilities that need explicit targets (e.g. Solitary Exile's
    Deploy "Void another target card"). Mirrors the client class exactly:
    PlayerId, AbilityInstanceIds (List<long>), AbilityTemplateIds
    (List<ResourceId>), SourceCardIds (List<SessionCardId>). The client shows
    BattleStateTriggeredAbilities -> BattleStateConfigureAbility picker and
    returns the chosen targets in a SetAbilityActivationDataTransaction."""
    CLASS_ID = 39

    def __init__(self):
        super().__init__()
        self.player_id = UID.invalid()
        self.ability_instance_ids: List[int] = []
        self.ability_template_ids: List[ResourceId] = []
        self.source_card_ids: List[SessionCardId] = []

    def to_byte_array(self) -> bytes:
        self.begin_write()
        s = self.ser
        s.add_uid(self.player_id)
        s.add_list_long(self.ability_instance_ids)
        s.add_list_resource_id(self.ability_template_ids)
        s.add_list_scid(self.source_card_ids)
        return self.end_write()


class CardsRevealedSessionEventArgs(SessionEventArgs):
    """Class 51 — the controller reveals cards from their deck (e.g. "look at
    three random cards from your deck").  Mirrors the client class exactly:
    PlayerId, SessionCardIds, Collections, OwningPlayers, Positions,
    AbilityInstanceId, Inactive."""
    CLASS_ID = 51

    def __init__(self):
        super().__init__()
        self.player_id = UID.invalid()
        self.session_card_ids: List[SessionCardId] = []
        self.collections: List[int] = []
        self.owning_players: List[UID] = []
        self.positions: List[int] = []
        self.ability_instance_id = 0
        self.inactive = False

    def to_byte_array(self) -> bytes:
        self.begin_write()
        s = self.ser
        s.add_uid(self.player_id)
        s.add_list_scid(self.session_card_ids)
        s.add_list_int(self.collections)
        s.add_list_uid(self.owning_players)
        s.add_list_int(self.positions)
        s.add_long(self.ability_instance_id)
        s.add_bool(self.inactive)
        return self.end_write()


class AttackDeclaredSessionEventArgs(SessionEventArgs):
    """Class 27 — a troop was declared as an attacker."""
    CLASS_ID = 27

    def __init__(self):
        super().__init__()
        self.combat_id = CombatId()
        self.attacking_player_id = UID.invalid()
        self.defending_card_id = SessionCardId()
        self.attacking_card_id = SessionCardId()
        self.forced_on_reconnect = False

    def to_byte_array(self) -> bytes:
        self.begin_write()
        s = self.ser
        s.add_combat_id(self.combat_id)
        s.add_uid(self.attacking_player_id)
        s.add_scid(self.defending_card_id)
        s.add_scid(self.attacking_card_id)
        s.add_bool(self.forced_on_reconnect)
        return self.end_write()


class BlockersAssignedSessionEventArgs(SessionEventArgs):
    """Class 28 — blockers assigned for one attacker."""
    CLASS_ID = 28

    def __init__(self):
        super().__init__()
        self.combat_id = CombatId()
        self.attacker_id = SessionCardId()
        self.defender_id = SessionCardId()
        self.blocking_card_ids: List[SessionCardId] = []

    def to_byte_array(self) -> bytes:
        self.begin_write()
        s = self.ser
        s.add_combat_id(self.combat_id)
        s.add_scid(self.attacker_id)
        s.add_scid(self.defender_id)
        s.add_list_scid(self.blocking_card_ids)
        return self.end_write()


class CombatPhaseResolvedSessionEventArgs(SessionEventArgs):
    """Class 29 — a combat damage phase (standard/first-strike) resolved."""
    CLASS_ID = 29

    def __init__(self):
        super().__init__()
        self.combat_id = CombatId()
        self.attacker_id = SessionCardId()
        self.defender_id = SessionCardId()
        self.blocking_card_ids: List[SessionCardId] = []
        self.combat_phase = ECombatPhase.None_

    def to_byte_array(self) -> bytes:
        self.begin_write()
        s = self.ser
        s.add_combat_id(self.combat_id)
        s.add_scid(self.attacker_id)
        s.add_scid(self.defender_id)
        s.add_list_scid(self.blocking_card_ids)
        s.add_enum_int(self.combat_phase)
        return self.end_write()


class BeginCombatResolutionSessionEventArgs(SessionEventArgs):
    """Class 30 — combat resolution begins (no fields)."""
    CLASS_ID = 30

    def to_byte_array(self) -> bytes:
        self.begin_write()
        return self.end_write()


class EndCombatResolutionSessionEventArgs(SessionEventArgs):
    """Class 31 — combat resolution ends (no fields)."""
    CLASS_ID = 31

    def to_byte_array(self) -> bytes:
        self.begin_write()
        return self.end_write()


class CombatRemovedSessionEventArgs(SessionEventArgs):
    """Class 32 — a combat ended / was removed."""
    CLASS_ID = 32

    def __init__(self):
        super().__init__()
        self.combat_id = CombatId()
        self.attacker_id = SessionCardId()
        self.defender_id = SessionCardId()
        self.blocking_card_ids: List[SessionCardId] = []

    def to_byte_array(self) -> bytes:
        self.begin_write()
        s = self.ser
        s.add_combat_id(self.combat_id)
        s.add_scid(self.attacker_id)
        s.add_scid(self.defender_id)
        s.add_list_scid(self.blocking_card_ids)
        return self.end_write()


class PlayerCurrentResourcePoolChangedSessionEventArgs(SessionEventArgs):
    CLASS_ID = 33

    def __init__(self):
        super().__init__()
        self.player_id = UID.invalid()
        self.operation = 1  # Add
        self.delta = 0
        self.new_value = 0

    def to_byte_array(self) -> bytes:
        self.begin_write()
        s = self.ser
        s.add_uid(self.player_id)
        s.add_enum_int(self.operation)
        s.add_int(self.delta)
        s.add_int(self.new_value)
        return self.end_write()


class PlayerTotalResourcePoolChangedSessionEventArgs(SessionEventArgs):
    CLASS_ID = 34

    def __init__(self):
        super().__init__()
        self.player_id = UID.invalid()
        self.operation = 1  # Add
        self.delta = 0
        self.new_value = 0

    def to_byte_array(self) -> bytes:
        self.begin_write()
        s = self.ser
        s.add_uid(self.player_id)
        s.add_enum_int(self.operation)
        s.add_int(self.delta)
        s.add_int(self.new_value)
        return self.end_write()


class PlayerResourceThresholdChangedSessionEventArgs(SessionEventArgs):
    CLASS_ID = 35

    def __init__(self):
        super().__init__()
        self.player_id = UID.invalid()
        self.color = ECardShards.Unknown
        self.operation = 1  # Add
        self.delta = 0
        self.new_value = 0

    def to_byte_array(self) -> bytes:
        self.begin_write()
        s = self.ser
        s.add_uid(self.player_id)
        s.add_enum_int(self.color)
        s.add_enum_int(self.operation)
        s.add_int(self.delta)
        s.add_int(self.new_value)
        return self.end_write()


class ChampionChargePointsChangedSessionEventArgs(SessionEventArgs):
    CLASS_ID = 36

    def __init__(self):
        super().__init__()
        self.player_id = UID.invalid()
        self.operation = 1  # Add
        self.delta = 0
        self.new_value = 0

    def to_byte_array(self) -> bytes:
        self.begin_write()
        s = self.ser
        s.add_uid(self.player_id)
        s.add_enum_int(self.operation)
        s.add_int(self.delta)
        s.add_int(self.new_value)
        return self.end_write()


class ChampionSpellPointsChangedSessionEventArgs(SessionEventArgs):
    CLASS_ID = 37

    def __init__(self):
        super().__init__()
        self.player_id = UID.invalid()
        self.operation = 1  # Add
        self.delta = 0
        self.new_value = 0

    def to_byte_array(self) -> bytes:
        self.begin_write()
        s = self.ser
        s.add_uid(self.player_id)
        s.add_enum_int(self.operation)
        s.add_int(self.delta)
        s.add_int(self.new_value)
        return self.end_write()


class ChampionHealthChangedSessionEventArgs(SessionEventArgs):
    CLASS_ID = 38

    def __init__(self):
        super().__init__()
        self.player_id = UID.invalid()
        self.old_damage_value = 0
        self.new_damage_value = 0

    def to_byte_array(self) -> bytes:
        self.begin_write()
        s = self.ser
        s.add_uid(self.player_id)
        s.add_int(self.old_damage_value)
        s.add_int(self.new_damage_value)
        return self.end_write()


class TopOfChainResolvedSessionEventArgs(SessionEventArgs):
    """The top of the chain resolved (class 41). Mirrors the client."""
    CLASS_ID = 41

    def __init__(self):
        super().__init__()
        self.ability_instance_id = 0

    def to_byte_array(self) -> bytes:
        self.begin_write()
        s = self.ser
        s.add_long(self.ability_instance_id)
        return self.end_write()


class RemovedTopOfChainSessionEventArgs(SessionEventArgs):
    """The top of the chain was removed (class 42). Mirrors the client."""
    CLASS_ID = 42

    def __init__(self):
        super().__init__()
        self.ability_instance_id = 0

    def to_byte_array(self) -> bytes:
        self.begin_write()
        s = self.ser
        s.add_long(self.ability_instance_id)
        return self.end_write()


class DeckCreatedSessionEventArgs(SessionEventArgs):
    CLASS_ID = 47

    def __init__(self):
        super().__init__()
        self.player_id = UID.invalid()
        self.session_card_ids: List[SessionCardId] = []
        self.deck_sleeve_id = ResourceId.invalid()
        self.gameboard_id = ResourceId.invalid()
        self.coin_id = ResourceId.invalid()

    def to_byte_array(self) -> bytes:
        self.begin_write()
        self.ser.add_uid(self.player_id)
        self.ser.add_list_scid(self.session_card_ids)
        self.ser.add_resource_id(self.deck_sleeve_id)
        self.ser.add_resource_id(self.gameboard_id)
        self.ser.add_resource_id(self.coin_id)
        return self.end_write()


class GreenLightSessionEventArgs(SessionEventArgs):
    CLASS_ID = 48

    def __init__(self):
        super().__init__()
        self.player_id = UID.invalid()
        self.context = EPriorityContext.Normal

    def to_byte_array(self) -> bytes:
        self.begin_write()
        self.ser.add_uid(self.player_id)
        self.ser.add_enum_int(self.context)
        return self.end_write()


class CardMovedSessionEventArgs(SessionEventArgs):
    CLASS_ID = 50

    def __init__(self):
        super().__init__()
        self.session_card_id = SessionCardId()
        self.player_id = UID.invalid()
        self.collection = ECardCollections.None_
        self.location = ECardLocations.Unknown
        self.index = 0

    def to_byte_array(self) -> bytes:
        self.begin_write()
        self.ser.add_scid(self.session_card_id)
        self.ser.add_uid(self.player_id)
        self.ser.add_enum_int(self.collection)
        self.ser.add_enum_int(self.location)
        self.ser.add_int(self.index)
        return self.end_write()


class ReconnectDoneSessionEventArgs(SessionEventArgs):
    """Class 53 — marks the end of a reconnect state snapshot."""
    CLASS_ID = 53

    def to_byte_array(self) -> bytes:
        self.begin_write()
        return self.end_write()


class CardCountersChangedSessionEventArgs(SessionEventArgs):
    """Class 54 — notifies the UI that a card counter changed."""
    CLASS_ID = 54

    def __init__(self):
        super().__init__()
        self.session_card_id = SessionCardId()
        self.card_counter_template_id = ResourceId.invalid()
        self.new_value = 0
        self.old_value = 0

    def to_byte_array(self) -> bytes:
        self.begin_write()
        self.ser.add_scid(self.session_card_id)
        self.ser.add_resource_id(self.card_counter_template_id)
        self.ser.add_int(self.new_value)
        self.ser.add_int(self.old_value)
        return self.end_write()


class CombatsThatNeedDamageSessionEventArgs(SessionEventArgs):
    """Class 61 — combats awaiting damage assignment."""
    CLASS_ID = 61

    def __init__(self):
        super().__init__()
        self.player_id = UID.invalid()
        self.combats: List[SessionEventArgs] = []

    def to_byte_array(self) -> bytes:
        self.begin_write()
        s = self.ser
        s.add_uid(self.player_id)
        s.add_list_events(self.combats)
        return self.end_write()


class CombatListingSessionEventArgs(SessionEventArgs):
    """Class 62 — pushes the current combat pairings to the client."""
    CLASS_ID = 62

    def __init__(self):
        super().__init__()
        self.player_id = UID.invalid()
        self.combats: List[SessionEventArgs] = []

    def to_byte_array(self) -> bytes:
        self.begin_write()
        s = self.ser
        s.add_uid(self.player_id)
        s.add_list_events(self.combats)
        return self.end_write()


class CombatSessionEventArgs(SessionEventArgs):
    """Class 63 — one attacker/blocker pairing for the combat UI."""
    CLASS_ID = 63

    def __init__(self):
        super().__init__()
        self.player_id = UID.invalid()
        self.id = CombatId()
        self.attacker = SessionCardId()
        self.blockers: List[SessionCardId] = []

    def to_byte_array(self) -> bytes:
        self.begin_write()
        s = self.ser
        s.add_uid(self.player_id)
        s.add_combat_id(self.id)
        s.add_scid(self.attacker)
        s.add_list_scid(self.blockers)
        return self.end_write()


class CardUpdatedSessionEventArgs(SessionEventArgs):
    CLASS_ID = 64

    def __init__(self):
        super().__init__()
        self.player_id = UID.invalid()
        self.session_card_id = SessionCardId()
        # Stats
        self.cost = 0; self.armor = 0; self.current_armor = 0
        self.rage = 0; self.dmult = 0; self.cdmult = 0
        self.attack = 0; self.defense = 0; self.min_const = 0
        self.tunneling = 0; self.min_limited = 0; self.escalation = 0
        self.lethal = False; self.extended_art = False
        self.controller = UID.invalid()
        self.sub_type = ""
        self.card_id = ResourceId.invalid()
        self.orig_template = ResourceId.invalid()
        self.affected_id = ResourceId.invalid()
        self.card_type = ECardTypes.Unknown
        self.state = ECardStates.None_
        self.ai_states = EAICardStates.None_
        self.attributes = ECardAttributes.Unknown
        self.collection = ECardCollections.None_
        self.abilities: List[ResourceId] = []
        self.intger_vars: Dict[str, int] = {}
        self.int_attrs: Dict[str, int] = {}
        self.counter_templates: List[ResourceId] = []
        self.counter_counts: List[int] = []
        self.gems = EGemTypesNew.Unknown
        self.threshold_list: List[int] = []
        self.nulling = False
        self.related_cards: List[SessionCardId] = []
        self.string_attrs: Dict[str, str] = {}
        self.activation_cost_mods = {}
        self.charge_point_cost_mods = {}
        self.spell_point_cost_mods = {}
        self.uses_per_game_counts = {}
        self.cooldown_counts = {}
        self.damage_shield = False
        self.affecting_abilities: List[ResourceId] = []
        self.feral = False

    def to_byte_array(self) -> bytes:
        self.begin_write()
        s = self.ser
        s.add_uid(self.player_id)
        s.add_scid(self.session_card_id)
        s.add_int(self.cost); s.add_int(self.armor); s.add_int(self.current_armor)
        s.add_int(self.rage); s.add_int(self.dmult); s.add_int(self.cdmult)
        s.add_int(self.attack); s.add_int(self.defense)
        s.add_int(self.min_const); s.add_int(self.tunneling); s.add_int(self.min_limited)
        s.add_int(self.escalation)
        s.add_bool(self.lethal); s.add_bool(self.extended_art)
        s.add_uid(self.controller)
        s.add_string(self.sub_type)
        s.add_resource_id(self.card_id)
        s.add_resource_id(self.orig_template)
        s.add_resource_id(self.affected_id)
        s.add_enum_int(self.card_type)
        s.add_enum_int(self.state)
        s.add_enum_int(self.ai_states)
        s.add_enum_int(self.attributes)
        s.add_enum_int(self.collection)
        s.add_list_resource_id(self.abilities)
        s.add_dict_str_int(self.intger_vars)
        s.add_dict_str_int(self.int_attrs)
        s.add_list_resource_id(self.counter_templates)
        s.add_list_int(self.counter_counts)
        s.add_enum_ulong(self.gems)
        s.add_list_int(self.threshold_list)
        s.add_bool(self.nulling)
        s.add_list_scid(self.related_cards)
        s.add_dict_str_str(self.string_attrs)
        # Dictionaries of ResourceId->int (serialized as pairs of ResourceId + int)
        self._write_dict_rid_int(self.activation_cost_mods)
        self._write_dict_rid_int(self.charge_point_cost_mods)
        self._write_dict_rid_int(self.spell_point_cost_mods)
        self._write_dict_rid_int(self.uses_per_game_counts)
        self._write_dict_rid_int(self.cooldown_counts)
        s.add_bool(self.damage_shield)
        s.add_list_resource_id(self.affecting_abilities)
        s.add_bool(self.feral)
        return self.end_write()

    def _write_dict_rid_int(self, d):
        s = self.ser
        s.w.write_int32(len(d))
        for k, v in d.items():
            k.write(s.w)
            s.w.write_int32(v)


class PlayerUpdatedSessionEventArgs(SessionEventArgs):
    CLASS_ID = 65

    def __init__(self):
        super().__init__()
        self.player_id = UID.invalid()
        self.health = 20; self.charges = 0
        self.resources = 0; self.turn_number = 0
        self.total_resources = 0
        self.remaining_time_h = 0; self.remaining_time_m = 25; self.remaining_time_s = 0
        self.threshold_values: List[int] = [0, 0, 0, 0, 0, 0]  # colorless, blood, ruby, sapphire, wild, diamond
        self.champion_id = SessionCardId()
        self.thresholds: List[int] = []
        self.max_hand_size = 7
        self.can_see_enemy_hand = False
        self.can_see_enemy_underground = False
        self.deck_sleeve_id = ResourceId.invalid()
        self.spell_points = 0

    def to_byte_array(self) -> bytes:
        self.begin_write()
        s = self.ser
        s.add_uid(self.player_id)
        s.add_int(self.health)
        s.add_int(self.charges)
        s.add_int(self.resources)
        s.add_int(self.turn_number)
        s.add_int(self.total_resources)
        s.add_int(self.remaining_time_h)
        s.add_int(self.remaining_time_m)
        s.add_int(self.remaining_time_s)
        s.add_list_int(self.threshold_values)
        s.add_scid(self.champion_id)
        s.add_list_int(self.thresholds)
        s.add_int(self.max_hand_size)
        s.add_bool(self.can_see_enemy_hand)
        s.add_bool(self.can_see_enemy_underground)
        s.add_resource_id(self.deck_sleeve_id)
        s.add_int(self.spell_points)
        return self.end_write()


class CostInstanceSessionEventArgs(SessionEventArgs):
    """A cost the player must pay to activate an ability option (class 66).

    Delivered inside OptionInstanceSessionEventArgs.TargetInstances. The client
    (PlayerOptions.cs:125) reads it into a CostInstance; a DiscardAbilityCostType
    cost prompts the 'discard a card' picker (BattleStateAssignXCost).
    """
    CLASS_ID = 66

    def __init__(self):
        super().__init__()
        self.min = 0
        self.max = 0
        self.cost_type = 0
        self.targets: List[SessionCardId] = []
        self.target_template_id = ResourceId.invalid()

    def to_byte_array(self) -> bytes:
        self.begin_write()
        s = self.ser
        s.add_int(self.min)
        s.add_int(self.max)
        s.add_int(self.cost_type)
        s.add_list_scid(self.targets)
        s.add_resource_id(self.target_template_id)
        return self.end_write()


class TargetInstanceSessionEventArgs(SessionEventArgs):
    """A target the player must choose when activating an ability option (class 67).

    Delivered inside OptionInstanceSessionEventArgs.TargetInstances. The client
    (PlayerOptions.cs:119) reads it into a TargetInstance; a manual target with
    hand cards prompts the card-picker (BattleStateAssignTargets) — used for
    choose-and-discard effects like Soothsaying.
    """
    CLASS_ID = 67

    def __init__(self):
        super().__init__()
        self.target_index = 0
        self.target_id = ResourceId.invalid()
        self.targets: List[SessionCardId] = []
        self.additional_targets: List[SessionCardId] = []

    def to_byte_array(self) -> bytes:
        self.begin_write()
        s = self.ser
        s.add_int(self.target_index)
        s.add_resource_id(self.target_id)
        s.add_list_scid(self.targets)
        s.add_list_scid(self.additional_targets)
        return self.end_write()


class CostInstanceSessionEventArgs(SessionEventArgs):
    """A card cost the player must pay when activating an ability option
    (class 66).  Delivered inside OptionInstanceSessionEventArgs.TargetInstances
    — the client's PlayerOptions.Update splits TargetInstance (class 67) from
    CostInstance (class 66), and BattleStateAssignXCost reads GetCostsFor(...)
    to prompt for void/sacrifice/exhaust costs (e.g. Bun'jitsu's "Void two
    ready troops you control").  Field order mirrors the client exactly.
    """
    CLASS_ID = 66

    def __init__(self):
        super().__init__()
        self.min_target_count = 1
        self.max_target_count = 1
        self.cost_type = 0  # EAbilityCostType
        self.targets: List[SessionCardId] = []
        self.target_template_id = ResourceId.invalid()

    def to_byte_array(self) -> bytes:
        self.begin_write()
        s = self.ser
        s.add_int(self.min_target_count)
        s.add_int(self.max_target_count)
        s.add_enum_int(self.cost_type)
        s.add_list_scid(self.targets)
        s.add_resource_id(self.target_template_id)
        return self.end_write()


class OptionInstanceSessionEventArgs(SessionEventArgs):
    CLASS_ID = 68

    def __init__(self):
        super().__init__()
        self.opt_id = ResourceId.invalid()
        self.target_ids: List[ResourceId] = []
        self.target_instances: List = []
        self.min_target_counts: List[int] = []
        self.max_target_counts: List[int] = []

    def to_byte_array(self) -> bytes:
        self.begin_write()
        s = self.ser
        s.add_resource_id(self.opt_id)
        s.add_list_resource_id(self.target_ids)
        s.add_list_events(self.target_instances)
        s.add_list_int(self.min_target_counts)
        s.add_list_int(self.max_target_counts)
        return self.end_write()


class PlayerOptionSessionEventArgs(SessionEventArgs):
    CLASS_ID = 69

    def __init__(self):
        super().__init__()
        self.card = SessionCardId()
        self.instances = []
        self.state = ECardUsage.Play

    def to_byte_array(self) -> bytes:
        self.begin_write()
        s = self.ser
        s.add_scid(self.card)
        s.add_list_events(self.instances)
        s.add_enum_int(self.state)
        return self.end_write()


class PlayerOptionListSessionEventArgs(SessionEventArgs):
    CLASS_ID = 70

    def __init__(self):
        super().__init__()
        self.player_id = UID.invalid()
        self.options: List[SessionEventArgs] = []

    def to_byte_array(self) -> bytes:
        self.begin_write()
        self.ser.add_uid(self.player_id)
        self.ser.add_list_events(self.options)
        return self.end_write()


class BulkSessionEventSessionEventArgs(SessionEventArgs):
    CLASS_ID = 71

    def __init__(self):
        super().__init__()
        self.player_id = UID.invalid()
        self.event_list: List[SessionEventArgs] = []

    def to_byte_array(self) -> bytes:
        self.begin_write()
        self.ser.add_uid(self.player_id)
        self.ser.add_list_events(self.event_list)
        return self.end_write()


class ChainEmptySessionEventArgs(SessionEventArgs):
    """The chain emptied (class 77). Mirrors the client (no fields)."""
    CLASS_ID = 77

    def to_byte_array(self) -> bytes:
        self.begin_write()
        return self.end_write()


class ShowTipSessionEventArgs(SessionEventArgs):
    CLASS_ID = 80

    def __init__(self):
        super().__init__()
        self.tip = ""
        self.button = False

    def to_byte_array(self) -> bytes:
        self.begin_write()
        self.ser.add_string(self.tip)
        self.ser.add_bool(self.button)
        return self.end_write()


class SkipSetupSessionEventArgs(SessionEventArgs):
    CLASS_ID = 82

    def __init__(self):
        super().__init__()
        self.skip = True

    def to_byte_array(self) -> bytes:
        self.begin_write()
        self.ser.add_bool(self.skip)
        return self.end_write()


class DisableInterfaceSessionEventArgs(SessionEventArgs):
    CLASS_ID = 83

    def __init__(self):
        super().__init__()
        self.disabled = True

    def to_byte_array(self) -> bytes:
        self.begin_write()
        self.ser.add_bool(self.disabled)
        return self.end_write()


class NetworkPacketSessionEventArgs(SessionEventArgs):
    CLASS_ID = 255

    def __init__(self):
        super().__init__()
        self.player_id = UID.invalid()
        self.event_ids: List[int] = []
        self.event_data: List[bytes] = []

    def add_event(self, arg: SessionEventArgs):
        self.event_ids.append(arg.CLASS_ID)
        self.event_data.append(arg.to_byte_array())

    def to_byte_array(self) -> bytes:
        raise NotImplementedError("NetworkPacket is NOT serialized by custom binary format")


class AnimationTriggerSessionEventArgs(SessionEventArgs):
    """Game.Shared.AnimationTriggerSessionEventArgs (client class 76)."""
    CLASS_ID = 76

    def __init__(self):
        super().__init__()
        self.trigger_value = 0

    def to_byte_array(self) -> bytes:
        self.begin_write()
        self.ser.add_enum_int(self.trigger_value)
        return self.end_write()


# The following protocol events are present in the shipped client but were
# previously absent from the server event model.  Keep their field order
# identical to Game.Shared.Serializer on the client.

class ChessTimerUpdatedSessionEventArgs(SessionEventArgs):
    CLASS_ID = 4

    def __init__(self):
        super().__init__()
        self.player_id = UID.invalid()
        self.time = (0, 0, 0)

    def to_byte_array(self) -> bytes:
        self.begin_write()
        self.ser.add_uid(self.player_id)
        self.ser.add_timespan(self.time)
        return self.end_write()


class CardDestroyedSessionEventArgs(SessionEventArgs):
    CLASS_ID = 8

    def __init__(self):
        super().__init__()
        self.player_id = UID.invalid()
        self.session_card_id = SessionCardId()
        self.responsible_card_id = SessionCardId()

    def to_byte_array(self) -> bytes:
        self.begin_write()
        self.ser.add_uid(self.player_id)
        self.ser.add_scid(self.session_card_id)
        self.ser.add_scid(self.responsible_card_id)
        return self.end_write()


class CardVoidedSessionEventArgs(SessionEventArgs):
    CLASS_ID = 11

    def __init__(self):
        super().__init__()
        self.player_id = UID.invalid()
        self.session_card_id = SessionCardId()

    def to_byte_array(self) -> bytes:
        self.begin_write()
        self.ser.add_uid(self.player_id)
        self.ser.add_scid(self.session_card_id)
        return self.end_write()


class CardGraveyardedSessionEventArgs(SessionEventArgs):
    CLASS_ID = 12

    def __init__(self):
        super().__init__()
        self.player_id = UID.invalid()
        self.session_card_id = SessionCardId()

    def to_byte_array(self) -> bytes:
        self.begin_write()
        self.ser.add_uid(self.player_id)
        self.ser.add_scid(self.session_card_id)
        return self.end_write()


class SpellCardCastSessionEventArgs(SessionEventArgs):
    CLASS_ID = 18

    def __init__(self):
        super().__init__()
        self.player_id = UID.invalid()
        self.session_card_id = SessionCardId()
        self.played_for_free = False

    def to_byte_array(self) -> bytes:
        self.begin_write()
        self.ser.add_uid(self.player_id)
        self.ser.add_scid(self.session_card_id)
        self.ser.add_bool(self.played_for_free)
        return self.end_write()


class ArtifactCardPlayedSessionEventArgs(SessionEventArgs):
    CLASS_ID = 19

    def __init__(self):
        super().__init__()
        self.player_id = UID.invalid()
        self.session_card_id = SessionCardId()

    def to_byte_array(self) -> bytes:
        self.begin_write()
        self.ser.add_uid(self.player_id)
        self.ser.add_scid(self.session_card_id)
        return self.end_write()


class AbilityCancelledSessionEventArgs(SessionEventArgs):
    CLASS_ID = 21

    def __init__(self):
        super().__init__()
        self.ability_instance_id = 0
        self.responsible_player_id = UID.invalid()

    def to_byte_array(self) -> bytes:
        self.begin_write()
        self.ser.add_long(self.ability_instance_id)
        self.ser.add_uid(self.responsible_player_id)
        return self.end_write()


class CardTappedSessionEventArgs(SessionEventArgs):
    CLASS_ID = 24

    def __init__(self):
        super().__init__()
        self.session_card_id = SessionCardId()

    def to_byte_array(self) -> bytes:
        self.begin_write()
        self.ser.add_scid(self.session_card_id)
        return self.end_write()


class CardUntappedSessionEventArgs(SessionEventArgs):
    CLASS_ID = 25

    def __init__(self):
        super().__init__()
        self.session_card_id = SessionCardId()

    def to_byte_array(self) -> bytes:
        self.begin_write()
        self.ser.add_scid(self.session_card_id)
        return self.end_write()


class CardPrimedSessionEventArgs(SessionEventArgs):
    CLASS_ID = 26

    def __init__(self):
        super().__init__()
        self.session_card_id = SessionCardId()

    def to_byte_array(self) -> bytes:
        self.begin_write()
        self.ser.add_scid(self.session_card_id)
        return self.end_write()


class EncounterCardsCreatedInZoneSessionEventArgs(SessionEventArgs):
    CLASS_ID = 43

    def __init__(self):
        super().__init__()
        self.player_id = UID.invalid()
        self.collection = ECardCollections.None_
        self.location = ECardLocations.Unknown
        self.card_list: List[SessionCardId] = []

    def to_byte_array(self) -> bytes:
        self.begin_write()
        self.ser.add_uid(self.player_id)
        self.ser.add_enum_int(self.collection)
        self.ser.add_enum_int(self.location)
        self.ser.add_list_scid(self.card_list)
        return self.end_write()


class CardTransformedSessionEventArgs(SessionEventArgs):
    CLASS_ID = 44

    def __init__(self):
        super().__init__()
        self.session_card_id = SessionCardId()
        self.card_template_id = ResourceId.invalid()
        self.is_replica = False
        self.gems = EGemTypesNew.Unknown

    def to_byte_array(self) -> bytes:
        self.begin_write()
        self.ser.add_scid(self.session_card_id)
        self.ser.add_resource_id(self.card_template_id)
        self.ser.add_bool(self.is_replica)
        self.ser.add_enum_ulong(self.gems)
        return self.end_write()


class CardRevertedSessionEventArgs(SessionEventArgs):
    CLASS_ID = 45

    def __init__(self):
        super().__init__()
        self.session_card_id = SessionCardId()
        self.card_template_id = ResourceId.invalid()

    def to_byte_array(self) -> bytes:
        self.begin_write()
        self.ser.add_scid(self.session_card_id)
        self.ser.add_resource_id(self.card_template_id)
        return self.end_write()


class EquipmentSetSessionEventArgs(SessionEventArgs):
    CLASS_ID = 46

    def __init__(self):
        super().__init__()
        self.player_id = UID.invalid()
        self.equipment_ids: List[ResourceId] = []

    def to_byte_array(self) -> bytes:
        self.begin_write()
        self.ser.add_uid(self.player_id)
        self.ser.add_list_resource_id(self.equipment_ids)
        return self.end_write()


class CardCollectionsMergedSessionEventArgs(SessionEventArgs):
    CLASS_ID = 49

    def __init__(self):
        super().__init__()
        self.player_id = UID.invalid()
        self.source = ECardCollections.None_
        self.destination = ECardCollections.None_

    def to_byte_array(self) -> bytes:
        self.begin_write()
        self.ser.add_uid(self.player_id)
        self.ser.add_enum_int(self.source)
        self.ser.add_enum_int(self.destination)
        return self.end_write()


def make_game_ended_packet(session_id, sender_uid, winners, losers):
    """Build a NetworkPacketSessionEventArgs carrying a GameEnded event.

    session_id is an int (the DB session id). winners/losers are lists of UID.
    The returned packet is ready for encode_sync_event() / the 3055 channel and
    produces byte-identical output to the hand-built struct used previously.
    """
    ev = GameEndedSessionEventArgs()
    ev.session_id = UID(session_id)
    ev.winners = winners
    ev.losers = losers
    nw = NetworkPacketSessionEventArgs()
    nw.session_id = UID(session_id)
    nw.player_id = sender_uid
    nw.add_event(ev)
    return nw
