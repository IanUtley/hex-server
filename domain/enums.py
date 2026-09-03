"""Hex TCG domain enum types (matching Game.Shared.Mechanics enums on the client)."""


class EAnimationTrigger:
    """Game.Shared.Mechanics.EAnimationTrigger wire values."""
    Invalid = 0
    CannonTalent = 1
    MageTalent = 2
    WarriorTalent = 3
    ClericTalent = 4
    RangerTalent = 5
    Kraken = 8


# ======================================================================
#  Turn Phases
# ======================================================================

class ETurnPhases:
    Unknown = 0
    NotPlaying = 1
    PreGame = 2
    PickGoesFirst = 3
    Mulligan = 4
    StartGame = 5
    StartTurn = 6
    Ready = 7
    Prep = 8
    Draw = 9
    FirstMainPhase = 10
    DeclareCombatPriorityWindow = 11
    DeclareAttack = 12
    DeclareAttackPriorityWindow = 13
    DeclareDefense = 14
    DeclareDefensePriorityWindow = 15
    AssignFirstStrikeDamage = 16
    FirstStrikePriorityWindow = 17
    AssignDamage = 18
    SecondMainPhase = 19
    EndPhase = 20
    Discard = 21
    EndTurn = 22
    Checksum = 23
    EndGame = 24


# ======================================================================
#  Card Types
# ======================================================================

class ECardTypes:
    Unknown = 0
    Champion = 1
    Troop = 2
    Gear = 4
    BasicAction = 8
    Resource = 16
    Artifact = 32
    QuickAction = 64
    Constant = 2048
    Token = 4096
    Quick = 8192


CARD_TYPE_BY_DB = {
    "Resource": ECardTypes.Resource,
    "Troop": ECardTypes.Troop,
    "Artifact": ECardTypes.Artifact,
    "BasicAction": ECardTypes.BasicAction,
    "QuickAction": ECardTypes.QuickAction,
    "Constant": ECardTypes.Constant,
    "Gear": ECardTypes.Gear,
    "Token": ECardTypes.Token,
    "Quick": ECardTypes.Quick,
}


def card_type_from_db(name):
    """Map a DB card_templates.card_type string to an ECardTypes value.

    Handles combined types ("Troop|Artifact", "Constant|Quick") by OR-ing the
    individual bits; unknown/absent types default to Troop.
    """
    if not name:
        return ECardTypes.Troop
    bits = 0
    for part in str(name).split("|"):
        bits |= CARD_TYPE_BY_DB.get(part.strip(), ECardTypes.Troop)
    return bits


# ======================================================================
#  Card Collections / Locations / States
# ======================================================================

class ECardCollections:
    None_ = 0
    Deck = 1
    Hand = 2
    Champions = 4
    Warzone = 8
    Discard = 16
    Void = 32
    PlayedResources = 64
    CastSpells = 128
    Underground = 256
    Choosing = 512
    Mod = 1024
    Simulacrum = 2048
    UI_Warzone = 4096
    UI_Constant = 8192


class ECardLocations:
    Unknown = 0
    Top = 1
    Bottom = 2


class ECardStates:
    None_ = 0
    Tapped = 1
    Blocking = 2
    Attacking = 4
    Damaged = 16
    Healed = 32
    Dead = 64
    HasAttacked = 128
    HasBlocked = 256
    EffectExpired = 512
    ZoneChangeReplacement = 1024
    Activated = 2048
    CameOutThisTurn = 8192
    StartedATurnOnYourSide = 16384


class EAICardStates:
    None_ = 0
    AbleToBlock = 1


# ======================================================================
#  Card Attributes (bit flags)
# ======================================================================

class ECardAttributes:
    """Game.Shared.Mechanics.ECardAttributes [Flags] (values match the client)."""
    Unknown = 0
    SpiritDrain = 1
    Flight = 2
    Speed = 4
    SkyGuard = 8
    Juggernaught = 16
    Steadfast = 32
    Immortal = 64
    SpellShield = 128
    Unique = 256
    CantAttack = 512
    CantBlock = 1024
    Defensive = 2048
    ForceAttack = 4096
    CantReadyAutomatically = 8192
    FirstStrike = 16384
    Rage = 32768
    MustBlock = 65536
    CantBeBlocked = 131072
    PreventCombatDamage = 262144
    PreventNonCombatDamage = 524288
    PreventAllDamage = 786432
    DualStrike = 1048576
    CantInflictCombatDamage = 2097152
    CantInflictNonCombatDamage = 4194304
    CantInflictAnyDamage = 6291456
    EntersPlayExhausted = 8388608
    Inspire = 16777216
    Escalation = 33554432
    DoesntReadyNextReadyStep = 67108864
    VoidsDamagedTroops = 134217728
    QuickAction = 268435456
    AllowYardInspire = 536870912
    MustBeBlocked = 1073741824
    Boon = -2147483648


# ======================================================================
#  Card Shards / Threshold
# ======================================================================

class ECardShards:
    Unknown = 0
    Colorless = 1
    Blood = 4
    Ruby = 8
    Sapphire = 16
    Wild = 32
    Diamond = 64


class EGemTypesNew:
    Unknown = 4611686018427387904
    Basic = 1


# ======================================================================
#  Combat / Priority
# ======================================================================

class ECombatPhase:
    """Game.Shared.Mechanics.ECombatPhase [Flags]."""
    None_ = 0
    Standard = 1
    FirstStrike = 2


class EPriorityContext:
    Unknown = 0
    Normal = 1
    Ready = 2
    OpponentsReady = 3
    ProcedeToCombat = 4
    ProcedeToOpponentsCombat = 5
    ResolveTopOfChain = 6
    ProcedeToSecondMain = 7
    ProcedeToOpponentsSecondMain = 8
    ProceedToEndTurn = 9
    ProceedToOpponentsEndTurn = 10
    EndPhase = 11
    EndOpponentsPhase = 12
    AutoPass = 13
    ResolveCombat = 14
    ProcedeToBlockers = 15
    ProcedeToMyBlockers = 16


class ECardUsage:
    None_ = 0
    Play = 1
    Activate = 2
    Attack = 4
    Defend = 8
    ForcedAttack = 16
    PlayForFree = 32


class ECardShard:
    """Single shard values (non-flags)."""
    Colorless = 0
    Blood = 1
    Ruby = 2
    Sapphire = 3
    Wild = 4
    Diamond = 5


# ======================================================================
#  Shard name mappings
# ======================================================================

SHARD_TO_COLOR = {
    "blood": ECardShard.Blood, "ruby": ECardShard.Ruby, "sapphire": ECardShard.Sapphire,
    "wild": ECardShard.Wild, "diamond": ECardShard.Diamond, "colorless": ECardShard.Colorless,
}

SHARD_TO_FLAG = {
    "blood": ECardShards.Blood, "ruby": ECardShards.Ruby, "sapphire": ECardShards.Sapphire,
    "wild": ECardShards.Wild, "diamond": ECardShards.Diamond,
}
