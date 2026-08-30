"""Compatibility re-exports for the Hex TCG private server.

All domain types previously defined directly in this module have moved into
the ``domain`` package.  This module re-exports every public name so existing
``import game_engine`` callers work unchanged.

New code should import from ``domain.<submodule>`` directly.
"""

from domain.binary_io import BinaryWriter, BinaryReader
from domain.types import UID, ResourceId, SessionCardId, CombatId
from domain.enums import (
    ETurnPhases, ECardTypes, ECardCollections, ECardLocations, ECardStates,
    EAICardStates, ECardAttributes, ECardShards, EGemTypesNew, ECombatPhase,
    EPriorityContext, ECardUsage, ECardShard,
    CARD_TYPE_BY_DB, card_type_from_db,
    SHARD_TO_COLOR, SHARD_TO_FLAG,
)
from domain.serializer import Serializer
from domain.events import (
    SessionEventArgs,
    GameStartedSessionEventArgs,
    GameEndedSessionEventArgs,
    TurnPhaseUpdatedSessionEventArgs,
    ChessTimerUpdatedSessionEventArgs,
    PlayerMulliganedHandSessionEventArgs,
    PlayerAcceptedStartingHandSessionEventArgs,
    CardDrawnSessionEventArgs,
    CardDiscardedSessionEventArgs,
    CardDestroyedSessionEventArgs,
    CardVoidedSessionEventArgs,
    CardGraveyardedSessionEventArgs,
    ChampionCardPlayedSessionEventArgs,
    TroopCardPlayedSessionEventArgs,
    ResourceCardPlayedSessionEventArgs,
    SpellCardCastSessionEventArgs,
    ArtifactCardPlayedSessionEventArgs,
    AbilityCancelledSessionEventArgs,
    AbilityPushedOnChainSessionEventArgs,
    AbilityActivationDataRequiredSessionEventArgs,
    CardTappedSessionEventArgs,
    CardUntappedSessionEventArgs,
    CardPrimedSessionEventArgs,
    TriggeredAbilityActivationDataRequiredSessionEventArgs,
    CardsRevealedSessionEventArgs,
    AttackDeclaredSessionEventArgs,
    BlockersAssignedSessionEventArgs,
    CombatPhaseResolvedSessionEventArgs,
    BeginCombatResolutionSessionEventArgs,
    EndCombatResolutionSessionEventArgs,
    CombatRemovedSessionEventArgs,
    PlayerCurrentResourcePoolChangedSessionEventArgs,
    PlayerTotalResourcePoolChangedSessionEventArgs,
    PlayerResourceThresholdChangedSessionEventArgs,
    ChampionChargePointsChangedSessionEventArgs,
    ChampionSpellPointsChangedSessionEventArgs,
    ChampionHealthChangedSessionEventArgs,
    TopOfChainResolvedSessionEventArgs,
    RemovedTopOfChainSessionEventArgs,
    EncounterCardsCreatedInZoneSessionEventArgs,
    CardTransformedSessionEventArgs,
    CardRevertedSessionEventArgs,
    EquipmentSetSessionEventArgs,
    DeckCreatedSessionEventArgs,
    GreenLightSessionEventArgs,
    CardCollectionsMergedSessionEventArgs,
    CardMovedSessionEventArgs,
    CardCountersChangedSessionEventArgs,
    CombatsThatNeedDamageSessionEventArgs,
    CombatListingSessionEventArgs,
    CombatSessionEventArgs,
    CardUpdatedSessionEventArgs,
    PlayerUpdatedSessionEventArgs,
    CostInstanceSessionEventArgs,
    TargetInstanceSessionEventArgs,
    OptionInstanceSessionEventArgs,
    PlayerOptionSessionEventArgs,
    PlayerOptionListSessionEventArgs,
    BulkSessionEventSessionEventArgs,
    ChainEmptySessionEventArgs,
    ShowTipSessionEventArgs,
    SkipSetupSessionEventArgs,
    DisableInterfaceSessionEventArgs,
    NetworkPacketSessionEventArgs,
    make_game_ended_packet,
)
from domain.game import CardDef, Game, parse_tutorial_script

import domain.constants as _dc

event_logger = _dc.event_logger
DISCARD_TARGET_TEMPLATE = _dc.DISCARD_TARGET_TEMPLATE
PLAY_CARD_ABILITY_TEMPLATE_ID = _dc.PLAY_CARD_ABILITY_TEMPLATE_ID
SELF_TARGET_TEMPLATE = _dc.SELF_TARGET_TEMPLATE
