"""CardDef, Game (event queue / tutorial engine), and script parser."""

import random as _random
from typing import List, Dict, Any, Optional, Tuple

from domain.types import UID, ResourceId, SessionCardId, CombatId
from domain.enums import *
from domain.events import *
# ``event_logger`` is assigned explicitly by hconnect_server at startup.  It
# is intentionally imported as a module-level callback because this module's
# Game objects are also used by Practice and Campaign; the installed callback
# decides which session types are replayable.
event_logger = None
from domain.constants import PLAY_CARD_ABILITY_TEMPLATE_ID


# ======================================================================
#  CardDef
# ======================================================================

class CardDef:
    """Definition of a card in the game."""
    def __init__(self, name: str, card_type: int = ECardTypes.Troop,
                 cost: int = 0, attack: int = 0, defense: int = 0,
                 shards: List[int] = None, abilities: List[ResourceId] = None,
                 attributes: int = ECardAttributes.Unknown):
        self.name = name
        self.card_type = card_type
        self.cost = cost
        self.attack = attack
        self.defense = defense
        self.shards = shards or []
        self.abilities = abilities or []
        self.attributes = attributes
        self.escalation = 0
        self.spell_point_cost_mods: Dict[ResourceId, int] = {}
        self.uses_per_game_counts: Dict[ResourceId, int] = {}
        self.orig_template: str = None
        self.counters: Dict[str, int] = {}
        self.related_cards: List[SessionCardId] = []
        self.gems = 0
        self.rage = 0
        self.int_attrs = {}

    @property
    def is_resource(self):
        return self.card_type == ECardTypes.Resource

    @property
    def is_troop(self):
        return self.card_type == ECardTypes.Troop


# ======================================================================
#  Game
# ======================================================================

class Game:
    """Tutorial game engine - scripted encounter vs AI."""

    CARD_DB = {
        "wild shard": CardDef("Wild Shard", ECardTypes.Resource, 0, 0, 0, [ECardShards.Wild]),
        "ruby shard": CardDef("Ruby Shard", ECardTypes.Resource, 0, 0, 0, [ECardShards.Ruby]),
        "blood shard": CardDef("Blood Shard", ECardTypes.Resource, 0, 0, 0, [ECardShards.Blood]),
        "sapphire shard": CardDef("Sapphire Shard", ECardTypes.Resource, 0, 0, 0, [ECardShards.Sapphire]),
        "cadaver beetle": CardDef("Cadaver Beetle", ECardTypes.Troop, 1, 1, 1),
        "arena brawler": CardDef("Arena Brawler", ECardTypes.Troop, 4, 5, 4),
        "shin'hare militia": CardDef("Shin'hare Militia", ECardTypes.Troop, 2, 2, 2),
        "skaldeer brawler": CardDef("Skaldeer Brawler", ECardTypes.Troop, 3, 2, 3),
        "grand squirrel titan": CardDef("Grand Squirrel Titan", ECardTypes.Troop, 6, 5, 5),
        "rotting buffalo": CardDef("Rotting Buffalo", ECardTypes.Troop, 2, 2, 3),
        "sepulchra warden": CardDef("Sepulchra Warden", ECardTypes.Troop, 3, 2, 4),
        "goliath spiderspawn": CardDef("Goliath Spiderspawn", ECardTypes.Troop, 5, 4, 5),
        "war brute": CardDef("War Brute", ECardTypes.Troop, 3, 3, 2),
        "warrior": CardDef("Warrior", ECardTypes.Troop, 0, 1, 1),
        "baby yeti": CardDef("Baby Yeti", ECardTypes.Troop, 0, 0, 1),
        "pummel": CardDef("Pummel", ECardTypes.BasicAction, 2),
        "firepower": CardDef("Firepower", ECardTypes.BasicAction, 3),
        "boonberries": CardDef("Boonberries", ECardTypes.BasicAction, 1),
        "nature reigns": CardDef("Nature Reigns", ECardTypes.BasicAction, 5),
        "call the grave": CardDef("Call the Grave", ECardTypes.BasicAction, 1),
        "oracle song": CardDef("Oracle Song", ECardTypes.BasicAction, 2),
        "growth potion": CardDef("Growth Potion", ECardTypes.BasicAction, 2),
        "mightsinger alyndra": CardDef("Mightsinger Alyndra", ECardTypes.Champion, 0, 0, 0, [ECardShards.Ruby, ECardShards.Wild]),
        "bertram cragraven": CardDef("Bertram Cragraven", ECardTypes.Champion, 0, 0, 0, [ECardShards.Sapphire, ECardShards.Blood]),
    }

    CARD_TYPE_TEMPLATES = {
        ECardTypes.Resource: "a2e5a8bc-f574-47a0-9b35-99d7a4c2f1e",
        ECardTypes.Troop: "b3f6b9cd-a685-58b1-9c46-99d7a4c2f1e3",
        ECardTypes.BasicAction: "c4a7ca0e-b796-69c2-ad57-99d7a4c2f1e4",
        ECardTypes.Champion: "d5b8db1f-c8a7-7ad3-be68-99d7a4c2f1e5",
    }

    def __init__(self, session_id: int, player_uid: UID, ai_uid: UID):
        self.session_id = UID(session_id)
        self.player_uid = player_uid
        self.ai_uid = ai_uid
        self.next_card_id = 1
        self.turn_number = 0
        self.current_phase = ETurnPhases.PreGame
        self.priority_player = player_uid
        self.active_player = player_uid

        self.player_health = 20
        self.ai_health = 20
        self.player_resources = 0
        self.ai_resources = 0
        self.player_total_resources = 0
        self.ai_total_resources = 0
        self.player_threshold: Dict[int, int] = {}
        self.ai_threshold: Dict[int, int] = {}
        self.player_champion_card_id: Optional[SessionCardId] = None
        self.ai_champion_card_id: Optional[SessionCardId] = None

        self.player_charges = 0
        self.ai_charges = 0
        self.player_spell_points = 0
        self.ai_spell_points = 0

        self.player_hand: List[SessionCardId] = []
        self.player_deck: List[Tuple[SessionCardId, CardDef]] = []
        self.player_warzone: List[SessionCardId] = []
        self.player_resources_played: List[SessionCardId] = []

        self.ai_hand: List[SessionCardId] = []
        self.ai_deck: List[Tuple[SessionCardId, CardDef]] = []
        self.ai_warzone: List[SessionCardId] = []
        self.ai_resources_played: List[SessionCardId] = []

        self.card_defs: Dict[SessionCardId, CardDef] = {}

        self.events: List[SessionEventArgs] = []

        self.script_commands: List[dict] = []
        self.script_index = 0
        self.interface_disabled = False

    def _new_card_id(self) -> SessionCardId:
        cid = SessionCardId(UID.make(1, self.next_card_id))
        self.next_card_id += 1
        return cid

    def _parse_deck(self, cards: List[str]) -> List[Tuple[str, int]]:
        result = []
        for c in cards:
            c = c.strip().lower()
            parts = c.strip("()").strip().split(" ", 1)
            if len(parts) == 2 and parts[0].isdigit():
                count = int(parts[0])
                name = parts[1]
                result.append((name, count))
            else:
                name = c.strip("()").strip()
                result.append((name, 1))
        return result

    def setup_deck(self, player_uid: UID, cards: List[str], champion: str):
        parsed = self._parse_deck(cards)
        is_player = player_uid == self.player_uid

        champ_def = self.CARD_DB.get(champion.lower())
        if not champ_def:
            champ_def = CardDef(champion, ECardTypes.Champion)

        champ_card_id = self._new_card_id()
        self.card_defs[champ_card_id] = champ_def

        if is_player:
            self.player_champion_card_id = champ_card_id
        else:
            self.ai_champion_card_id = champ_card_id

        deck_cards = []
        for name, count in parsed:
            card_def = self.CARD_DB.get(name)
            if not card_def:
                card_def = CardDef(name, ECardTypes.Troop, 0, 0, 0)
            for _ in range(count):
                cid = self._new_card_id()
                self.card_defs[cid] = card_def
                deck_cards.append(cid)

        if is_player:
            self.player_deck = [(cid, self.card_defs[cid]) for cid in deck_cards]
        else:
            self.ai_deck = [(cid, self.card_defs[cid]) for cid in deck_cards]

    def draw_cards(self, player_uid: UID, count: int):
        is_player = player_uid == self.player_uid
        deck = self.player_deck if is_player else self.ai_deck
        hand = self.player_hand if is_player else self.ai_hand

        drawn = []
        for _ in range(count):
            if not deck:
                break
            cid, cdef = deck.pop(0)
            hand.append(cid)
            drawn.append(cid)
        return drawn

    def _make_event(self, cls, session_id_val=None) -> SessionEventArgs:
        ev = cls()
        ev.session_id = UID(session_id_val) if session_id_val else self.session_id
        return ev

    def _push(self, ev: SessionEventArgs):
        self.events.append(ev)

    def push_champion_health_changed_if_missing(self, player_uid: UID,
                                                old_health: int,
                                                new_health: int,
                                                since: int = 0):
        """Queue the immediate champion-health event when an effect changed it.

        Most damage/heal leaves already emit class 38 themselves.  Some
        ability paths update the battle view directly, though, so keep the
        client HUD synchronized without duplicating an event that was already
        generated during this resolution.
        """
        old_health = int(old_health)
        new_health = int(new_health)
        if old_health == new_health:
            return False
        for event in self.events[since:]:
            if (isinstance(event, ChampionHealthChangedSessionEventArgs)
                    and event.player_id == player_uid
                    and event.new_damage_value == new_health):
                return False
        ev = self._make_event(ChampionHealthChangedSessionEventArgs)
        ev.player_id = player_uid
        ev.old_damage_value = old_health
        ev.new_damage_value = new_health
        self._push(ev)
        return True

    def push_card_updated(self, cid: SessionCardId, player_uid: UID,
                          collection: int = ECardCollections.None_,
                          card_type: int = ECardTypes.Unknown,
                          state: int = ECardStates.None_,
                          **kwargs):
        cdef = self.card_defs.get(cid)
        ev = self._make_event(CardUpdatedSessionEventArgs)
        ev.player_id = player_uid
        ev.session_card_id = cid
        ev.collection = collection
        ev.card_type = card_type or (cdef.card_type if cdef else ECardTypes.Unknown)
        ev.state = state
        ev.attack = kwargs.get('attack', cdef.attack if cdef else 0)
        ev.defense = kwargs.get('defense', cdef.defense if cdef else 0)
        ev.cost = kwargs.get('cost', cdef.cost if cdef else 0)
        ev.controller = player_uid
        ev.attributes = kwargs.get('attributes', cdef.attributes if cdef else ECardAttributes.Unknown)
        ev.int_attrs = dict(kwargs.get('int_attrs', getattr(cdef, 'int_attrs', {}) or {}))
        template_id = kwargs.get('template_id', None)
        if template_id:
            ev.card_id = ResourceId.from_str(template_id)
        orig_template = kwargs.get('orig_template', None) or (cdef.orig_template if cdef else None)
        if orig_template:
            ev.orig_template = ResourceId.from_str(orig_template)
        ev.gems = kwargs.get('gems', cdef.gems if cdef else 0)
        ev.rage = kwargs.get('rage', cdef.rage if cdef else 0)
        ev.nulling = kwargs.get('nulling', False)
        related = kwargs.get('related_cards', None)
        if related is None and cdef and cdef.related_cards:
            related = cdef.related_cards
        ev.related_cards = related or []
        ev.escalation = kwargs.get('escalation', 0)
        counters = kwargs.get('counters', None)
        if counters is None and cdef and cdef.counters:
            counters = cdef.counters
        if counters:
            ev.counter_templates = [ResourceId.from_str(g)
                                    for g in counters.keys()]
            ev.counter_counts = [int(c) for c in counters.values()]
        if cdef and cdef.shards:
            ev.threshold_list = cdef.shards
            tv = [0, 0, 0, 0, 0, 0]
            for s in cdef.shards:
                if 0 <= s < 6:
                    tv[s] += 1
            ev.threshold_values = tv
            ev.thresholds = [s for s in cdef.shards]
        if cdef and cdef.abilities:
            ev.abilities = list(cdef.abilities)
        if cdef and cdef.spell_point_cost_mods:
            ev.spell_point_cost_mods = dict(cdef.spell_point_cost_mods)
        if cdef and cdef.escalation:
            ev.escalation = cdef.escalation
        if cdef and cdef.card_type == ECardTypes.Resource:
            ev.sub_type = cdef.name
        self._push(ev)

    def push_card_counters_changed(self, cid: SessionCardId,
                                   counter_template: ResourceId,
                                   new_value: int, old_value: int):
        """Push the class-54 UI refresh event used by the client counter renderer."""
        ev = self._make_event(CardCountersChangedSessionEventArgs)
        ev.session_card_id = cid
        ev.card_counter_template_id = counter_template
        ev.new_value = int(new_value)
        ev.old_value = int(old_value)
        self._push(ev)

    def push_resource_card_played(self, cid: SessionCardId, player_uid: UID, free: bool = False):
        ev = self._make_event(ResourceCardPlayedSessionEventArgs)
        ev.player_id = player_uid
        ev.session_card_id = cid
        ev.free = free
        self._push(ev)

    def push_card_drawn(self, cid: SessionCardId, player_uid: UID, nth: int):
        ev = self._make_event(CardDrawnSessionEventArgs)
        ev.player_id = player_uid
        ev.session_card_id = cid
        ev.nth_card_drawn = nth
        self._push(ev)

    def push_card_discarded(self, cid: SessionCardId, player_uid: UID):
        ev = self._make_event(CardDiscardedSessionEventArgs)
        ev.player_id = player_uid
        ev.session_card_id = cid
        self._push(ev)

    def push_chess_timer_updated(self, player_uid: UID, time=(0, 0, 0)):
        ev = self._make_event(ChessTimerUpdatedSessionEventArgs)
        ev.player_id = player_uid
        ev.time = time
        self._push(ev)

    def push_card_destroyed(self, cid: SessionCardId, player_uid: UID,
                            responsible_cid: SessionCardId = None):
        ev = self._make_event(CardDestroyedSessionEventArgs)
        ev.player_id = player_uid
        ev.session_card_id = cid
        ev.responsible_card_id = responsible_cid or SessionCardId()
        self._push(ev)

    def push_card_voided(self, cid: SessionCardId, player_uid: UID):
        ev = self._make_event(CardVoidedSessionEventArgs)
        ev.player_id = player_uid
        ev.session_card_id = cid
        self._push(ev)

    def push_card_graveyarded(self, cid: SessionCardId, player_uid: UID):
        ev = self._make_event(CardGraveyardedSessionEventArgs)
        ev.player_id = player_uid
        ev.session_card_id = cid
        self._push(ev)

    def push_ability_on_chain(self, source_scid: SessionCardId,
                              ability_template_id: ResourceId,
                              ability_instance_id: int = 1,
                              target_card_ids: List[SessionCardId] = None,
                              ignores_chain: bool = False):
        ev = self._make_event(AbilityPushedOnChainSessionEventArgs)
        ev.source_card_id = source_scid
        ev.ability_template_id = ability_template_id
        ev.ability_instance_id = ability_instance_id
        ev.target_card_ids = target_card_ids or []
        ev.ignores_chain = ignores_chain
        self._push(ev)

    def push_top_of_chain_resolved(self, ability_instance_id: int):
        ev = self._make_event(TopOfChainResolvedSessionEventArgs)
        ev.ability_instance_id = ability_instance_id
        self._push(ev)

    def push_removed_top_of_chain(self, ability_instance_id: int):
        ev = self._make_event(RemovedTopOfChainSessionEventArgs)
        ev.ability_instance_id = ability_instance_id
        self._push(ev)

    def push_chain_empty(self):
        self._push(self._make_event(ChainEmptySessionEventArgs))

    def push_card_moved(self, cid: SessionCardId, player_uid: UID,
                        collection: int, location: int = ECardLocations.Top,
                        index: int = 0):
        ev = self._make_event(CardMovedSessionEventArgs)
        ev.session_card_id = cid
        ev.player_id = player_uid
        ev.collection = collection
        ev.location = location
        ev.index = index
        self._push(ev)

    def push_green_light(self, player_uid: UID, context: int = EPriorityContext.Normal):
        ev = self._make_event(GreenLightSessionEventArgs)
        ev.player_id = player_uid
        ev.context = context
        self._push(ev)

    def push_attack_declared(self, combat_id: CombatId, attacking_player: UID,
                             defending_card: SessionCardId, attacking_card: SessionCardId):
        ev = self._make_event(AttackDeclaredSessionEventArgs)
        ev.combat_id = combat_id
        ev.attacking_player_id = attacking_player
        ev.defending_card_id = defending_card
        ev.attacking_card_id = attacking_card
        ev.forced_on_reconnect = False
        self._push(ev)

    def push_blockers_assigned(self, combat_id: CombatId, attacker: SessionCardId,
                               defender: SessionCardId, blockers: List[SessionCardId]):
        ev = self._make_event(BlockersAssignedSessionEventArgs)
        ev.combat_id = combat_id
        ev.attacker_id = attacker
        ev.defender_id = defender
        ev.blocking_card_ids = list(blockers)
        self._push(ev)

    def push_combat_phase_resolved(self, combat_id: CombatId, attacker: SessionCardId,
                                   defender: SessionCardId, blockers: List[SessionCardId],
                                   phase: int = ECombatPhase.Standard):
        ev = self._make_event(CombatPhaseResolvedSessionEventArgs)
        ev.combat_id = combat_id
        ev.attacker_id = attacker
        ev.defender_id = defender
        ev.blocking_card_ids = list(blockers)
        ev.combat_phase = phase
        self._push(ev)

    def push_begin_combat_resolution(self):
        self._push(self._make_event(BeginCombatResolutionSessionEventArgs))

    def push_end_combat_resolution(self):
        self._push(self._make_event(EndCombatResolutionSessionEventArgs))

    def push_combat_removed(self, combat_id: CombatId, attacker: SessionCardId,
                            defender: SessionCardId, blockers: List[SessionCardId]):
        ev = self._make_event(CombatRemovedSessionEventArgs)
        ev.combat_id = combat_id
        ev.attacker_id = attacker
        ev.defender_id = defender
        ev.blocking_card_ids = list(blockers)
        self._push(ev)

    def push_combat_listing(self, player_uid: UID, combats: List[SessionEventArgs]):
        ev = self._make_event(CombatListingSessionEventArgs)
        ev.player_id = player_uid
        ev.combats = list(combats)
        self._push(ev)

    def push_combats_need_damage(self, player_uid: UID, combats: List[SessionEventArgs]):
        ev = self._make_event(CombatsThatNeedDamageSessionEventArgs)
        ev.player_id = player_uid
        ev.combats = list(combats)
        self._push(ev)

    def push_turn_phase(self, phase: int, active_uid: UID = None, prior_uid: UID = None):
        ev = self._make_event(TurnPhaseUpdatedSessionEventArgs)
        ev.turn_phase = phase
        ev.active_player_id = active_uid or self.active_player
        ev.priority_player_id = prior_uid or self.priority_player
        self.current_phase = phase
        self._push(ev)

    def push_player_wishes_to_draw_first(self, player_uid: UID):
        ev = self._make_event(PlayerWishesToDrawFirstSessionEventArgs)
        ev.player_id = player_uid
        self._push(ev)

    def push_player_wishes_to_play_first(self, player_uid: UID):
        ev = self._make_event(PlayerWishesToPlayFirstSessionEventArgs)
        ev.player_id = player_uid
        self._push(ev)

    def push_first_player_dictated(self, player_uid: UID):
        ev = self._make_event(FirstPlayerDictatedSessionEventArgs)
        ev.player_id = player_uid
        self._push(ev)

    def push_accepted_starting_hand(self, player_uid: UID, mulliganed: bool = False):
        ev = self._make_event(PlayerAcceptedStartingHandSessionEventArgs)
        ev.player_id = player_uid
        ev.mulliganed = mulliganed
        self._push(ev)

    def push_player_mulliganed_hand(self, player_uid: UID, count: int):
        ev = self._make_event(PlayerMulliganedHandSessionEventArgs)
        ev.player_id = player_uid
        ev.new_card_count = count
        self._push(ev)

    def push_player_option(self, scid: SessionCardId, usage: int):
        ev = self._make_event(PlayerOptionSessionEventArgs)
        ev.card = scid
        ev.state = usage
        ev.instances = []
        self._push(ev)

    def push_playable_cards(self, card_ids: List[SessionCardId], player_uid: UID):
        ev_list = self._make_event(PlayerOptionListSessionEventArgs)
        ev_list.player_id = player_uid
        for scid in card_ids:
            opt = PlayerOptionSessionEventArgs()
            opt.card = scid
            opt.state = ECardUsage.PlayForFree
            opt.instances = []
            ev_list.options.append(opt)
        self._push(ev_list)

    def push_player_updated(self, player_uid: UID, deck_sleeve_id: str = None,
                            champ_id: 'SessionCardId' = None):
        is_player = player_uid == self.player_uid
        ev = self._make_event(PlayerUpdatedSessionEventArgs)
        ev.player_id = player_uid
        ev.health = self.player_health if is_player else self.ai_health
        ev.resources = self.player_resources if is_player else self.ai_resources
        ev.total_resources = self.player_total_resources if is_player else self.ai_total_resources
        ev.turn_number = self.turn_number
        ev.charges = self.player_charges if is_player else self.ai_charges
        ev.spell_points = self.player_spell_points if is_player else self.ai_spell_points
        thresholds = self.player_threshold if is_player else self.ai_threshold
        ev.threshold_values = [
            thresholds.get(ECardShards.Colorless, 0),
            thresholds.get(ECardShards.Blood, 0),
            thresholds.get(ECardShards.Ruby, 0),
            thresholds.get(ECardShards.Sapphire, 0),
            thresholds.get(ECardShards.Wild, 0),
            thresholds.get(ECardShards.Diamond, 0),
        ]
        # Explicit champ_id wins; otherwise fall back to the Game's champion
        # card id for that side.  NOTE: the ternary binds looser than `or`, so
        # `champ_id or X if is_player else Y` would drop champ_id for the
        # opponent — always handle champ_id first.
        cid = champ_id
        if cid is None:
            cid = (self.player_champion_card_id if is_player
                   else self.ai_champion_card_id)
        if cid:
            ev.champion_id = cid
        if deck_sleeve_id:
            ev.deck_sleeve_id = ResourceId.from_str(deck_sleeve_id)
        self._push(ev)

    def push_show_tip(self, text: str, has_button: bool = False):
        ev = self._make_event(ShowTipSessionEventArgs)
        ev.tip = text
        ev.button = has_button
        self._push(ev)

    def push_skip_setup(self):
        ev = self._make_event(SkipSetupSessionEventArgs)
        ev.skip = True
        self._push(ev)

    def push_disable_interface(self, disabled: bool):
        ev = self._make_event(DisableInterfaceSessionEventArgs)
        ev.disabled = disabled
        self.interface_disabled = disabled
        self._push(ev)

    def push_animation_trigger(self, trigger: int):
        """Queue the class-76 animation event consumed by the client."""
        ev = self._make_event(AnimationTriggerSessionEventArgs)
        ev.trigger_value = int(trigger)
        self._push(ev)

    def push_game_started(self, champion_names=None, champion_template_ids=None,
                          player_first=None):
        ev = self._make_event(GameStartedSessionEventArgs)
        if player_first is None:
            ev.turn_order = [self.player_uid, self.ai_uid]
            _random.shuffle(ev.turn_order)
        elif player_first:
            ev.turn_order = [self.player_uid, self.ai_uid]
        else:
            ev.turn_order = [self.ai_uid, self.player_uid]
        ev.champion_names = champion_names or ["TestPlayer", "AI"]
        if champion_template_ids:
            ev.champion_template_ids = [ResourceId.from_str(g) for g in champion_template_ids]
        else:
            ev.champion_template_ids = [
                ResourceId.from_str("1d462ffb-0744-4996-804c-ba61b2c5c2f1"),
                ResourceId.from_str("f8f86969-2e47-4901-8c9e-7fbf8d859e22"),
            ]
        ev.sleeve_template_ids = []
        ev.board_template_ids = []
        ev.coin_template_ids = []
        ev.divisions = [1, 1]
        ev.seed_w = 0xABCDEF01
        ev.seed_z = 0x12345678
        self._push(ev)

    def push_champion_card_played(self, player_uid: UID, is_ai: bool, player_name: str, champ_card_id: SessionCardId):
        ev = self._make_event(ChampionCardPlayedSessionEventArgs)
        ev.player_id = player_uid
        ev.is_ai = is_ai
        ev.player_name = player_name
        ev.session_card_id = champ_card_id
        self._push(ev)

    def push_deck_created_with_cards(self, player_uid: UID, card_ids: List[SessionCardId]):
        ev = self._make_event(DeckCreatedSessionEventArgs)
        ev.player_id = player_uid
        ev.session_card_ids = card_ids
        ev.deck_sleeve_id = ResourceId.from_str("c508cdd3-77ad-4dbf-b1b4-b201eae5a690")
        ev.gameboard_id = ResourceId.from_str("3da4e3b7-e8e9-416a-b36c-e56cbc0aec47")
        ev.coin_id = ResourceId.from_str("c08eb0fa-8f98-43ee-afe7-25f05595cfb3")
        self._push(ev)

    def push_deck_created(self, player_uid: UID):
        is_player = player_uid == self.player_uid
        deck = self.player_deck if is_player else self.ai_deck
        ev = self._make_event(DeckCreatedSessionEventArgs)
        ev.player_id = player_uid
        ev.session_card_ids = [cid for cid, _ in deck]
        ev.deck_sleeve_id = ResourceId.from_str("ab34d0b0-25e4-4afb-3ed1-30aceed4c69c")
        ev.gameboard_id = ResourceId.from_str("a619833d-897f-b665-3645-3a26d55369a8")
        ev.coin_id = ResourceId.from_str("242b5ef9-4028-9b7a-a0bf-9e910f93d9a0")
        self._push(ev)

    def push_reconnect_done(self):
        ev = self._make_event(ReconnectDoneSessionEventArgs)
        self._push(ev)

    def push_troop_card_played(self, cid: SessionCardId, player_uid: UID):
        ev = self._make_event(TroopCardPlayedSessionEventArgs)
        ev.player_id = player_uid
        ev.session_card_id = cid
        self._push(ev)

    def push_spell_card_cast(self, cid: SessionCardId, player_uid: UID,
                             free: bool = False):
        ev = self._make_event(SpellCardCastSessionEventArgs)
        ev.player_id = player_uid
        ev.session_card_id = cid
        ev.played_for_free = free
        self._push(ev)

    def push_spell_card_played(self, cid: SessionCardId, player_uid: UID):
        ev = self._make_event(SpellCardPlayedSessionEventArgs)
        ev.player_id = player_uid
        ev.session_card_id = cid
        self._push(ev)

    def push_artifact_card_played(self, cid: SessionCardId, player_uid: UID):
        ev = self._make_event(ArtifactCardPlayedSessionEventArgs)
        ev.player_id = player_uid
        ev.session_card_id = cid
        self._push(ev)

    def push_ability_cancelled(self, instance_id: int, player_uid: UID):
        ev = self._make_event(AbilityCancelledSessionEventArgs)
        ev.ability_instance_id = instance_id
        ev.responsible_player_id = player_uid
        self._push(ev)

    def push_card_tapped(self, cid: SessionCardId):
        ev = self._make_event(CardTappedSessionEventArgs)
        ev.session_card_id = cid
        self._push(ev)

    def push_card_untapped(self, cid: SessionCardId):
        ev = self._make_event(CardUntappedSessionEventArgs)
        ev.session_card_id = cid
        self._push(ev)

    def push_card_primed(self, cid: SessionCardId):
        ev = self._make_event(CardPrimedSessionEventArgs)
        ev.session_card_id = cid
        self._push(ev)

    def push_encounter_cards_created(self, player_uid: UID, collection: int,
                                    location: int,
                                    card_ids: List[SessionCardId]):
        ev = self._make_event(EncounterCardsCreatedInZoneSessionEventArgs)
        ev.player_id = player_uid
        ev.collection = collection
        ev.location = location
        ev.card_list = list(card_ids or [])
        self._push(ev)

    def push_card_transformed(self, cid: SessionCardId, template_id: str,
                              is_replica: bool = False, gems: int = None):
        ev = self._make_event(CardTransformedSessionEventArgs)
        ev.session_card_id = cid
        ev.card_template_id = (template_id if isinstance(template_id, ResourceId)
                                else ResourceId.from_str(template_id))
        ev.is_replica = is_replica
        if gems is not None:
            ev.gems = gems
        self._push(ev)

    def push_card_reverted(self, cid: SessionCardId, template_id: str):
        ev = self._make_event(CardRevertedSessionEventArgs)
        ev.session_card_id = cid
        ev.card_template_id = (template_id if isinstance(template_id, ResourceId)
                                else ResourceId.from_str(template_id))
        self._push(ev)

    def push_equipment_set(self, player_uid: UID, equipment_ids: List[str]):
        ev = self._make_event(EquipmentSetSessionEventArgs)
        ev.player_id = player_uid
        ev.equipment_ids = [rid if isinstance(rid, ResourceId)
                            else ResourceId.from_str(rid)
                            for rid in (equipment_ids or [])]
        self._push(ev)

    def push_card_collections_merged(self, player_uid: UID, source: int,
                                     destination: int):
        ev = self._make_event(CardCollectionsMergedSessionEventArgs)
        ev.player_id = player_uid
        ev.source = source
        ev.destination = destination
        self._push(ev)

    def push_champion_options(self, player_uid: UID, champ_scid: SessionCardId,
                              ability_ids: List[ResourceId]):
        ev = self._make_event(PlayerOptionListSessionEventArgs)
        ev.player_id = player_uid
        opt = self._make_event(PlayerOptionSessionEventArgs)
        opt.card = champ_scid
        opt.state = ECardUsage.Activate
        for aid in ability_ids:
            inst = self._make_event(OptionInstanceSessionEventArgs)
            inst.opt_id = aid
            opt.instances.append(inst)
        ev.options.append(opt)
        self._push(ev)

    def add_champion_to_options(self, player_uid: UID, champ_scid: SessionCardId,
                                ability_ids: List[ResourceId], discard_costs: dict = None,
                                target_data: dict = None, cost_data: dict = None):
        """Append champion ability options (ECardUsage.Activate) to the most
        recent PlayerOptionList, one OptionInstance per affordable ability.

        ``target_data`` maps ability GUID -> [(target_template_id, [candidate
        card uids])] or (target_template_id, candidates, min, max,
        target_template_index). The five-item form preserves the ability's
        real target-template index — the client's target picker matches both
        MappingId and TemplateId, so without it a later explicit target can
        show zero valid targets.

        ``cost_data`` maps ability GUID -> [(target_template_id, EAbilityCostType,
        [candidate card uids], min, max)] for CARD costs (void/sacrifice/
        exhaust ...).  These are delivered as CostInstance events — the client's
        BattleStateAssignXCost reads GetCostsFor() and prompts for the cards
        (e.g. Bun'jitsu's "Void two ready troops you control").
        """
        if not self.events:
            return
        last_ev = self.events[-1]
        if not isinstance(last_ev, PlayerOptionListSessionEventArgs):
            return
        opt = self._make_event(PlayerOptionSessionEventArgs)
        opt.card = champ_scid
        opt.state = ECardUsage.Activate
        all_ids = list(ability_ids)
        for extra_g in (discard_costs or {}):
            if not any(str(a.guid) == extra_g for a in ability_ids):
                all_ids.append(ResourceId.from_str(extra_g))
        for aid in all_ids:
            inst = self._make_event(OptionInstanceSessionEventArgs)
            inst.opt_id = aid
            for entry in (target_data or {}).get(str(aid.guid), []):
                if len(entry) == 5:
                    # MappingId is the index in the ability's complete
                    # AbilityTargetTemplateIds list.  It is not necessarily
                    # the position in this filtered list: auto-targets and
                    # cost targets are deliberately omitted here.
                    tpl_id, candidates, mn, mx, target_index = entry
                elif len(entry) == 4:
                    tpl_id, candidates, mn, mx = entry
                    target_index = len(inst.target_instances)
                else:
                    tpl_id, candidates = entry
                    mn = mx = 1
                    target_index = len(inst.target_instances)
                inst.target_ids.append(ResourceId.from_str(tpl_id))
                inst.min_target_counts.append(mn)
                inst.max_target_counts.append(mx)
                tgt = self._make_event(TargetInstanceSessionEventArgs)
                tgt.target_index = int(target_index)
                tgt.target_id = ResourceId.from_str(tpl_id)
                tgt.targets = [SessionCardId(UID(int(u))) for u in candidates]
                inst.target_instances.append(tgt)
            dc = (discard_costs or {}).get(str(aid.guid))
            if dc:
                hand, tpl_id = dc
                inst.target_ids.append(ResourceId.from_str(tpl_id))
                inst.min_target_counts.append(1)
                inst.max_target_counts.append(1)
                tgt = self._make_event(TargetInstanceSessionEventArgs)
                tgt.target_index = 0
                tgt.target_id = ResourceId.from_str(tpl_id)
                tgt.targets = list(hand)
                inst.target_instances.append(tgt)
            for entry in (cost_data or {}).get(str(aid.guid), []):
                if len(entry) == 5:
                    tpl_id, ctype, candidates, mn, mx = entry
                else:
                    tpl_id, ctype, candidates = entry
                    mn = mx = 1
                cost_ev = self._make_event(CostInstanceSessionEventArgs)
                cost_ev.min_target_count = int(mn)
                cost_ev.max_target_count = int(mx)
                cost_ev.cost_type = int(ctype)
                cost_ev.targets = [SessionCardId(UID(int(u)))
                                   for u in (candidates or [])]
                cost_ev.target_template_id = ResourceId.from_str(tpl_id)
                inst.target_instances.append(cost_ev)
            opt.instances.append(inst)
        last_ev.options.append(opt)

    def push_options(self, player_uid: UID, available_cards: List[SessionCardId]):
        ev = self._make_event(PlayerOptionListSessionEventArgs)
        ev.player_id = player_uid
        for cid in available_cards:
            opt = self._make_event(PlayerOptionSessionEventArgs)
            opt.card = cid
            opt.state = ECardUsage.Play
            inst = self._make_event(OptionInstanceSessionEventArgs)
            inst.opt_id = ResourceId.from_str(PLAY_CARD_ABILITY_TEMPLATE_ID)
            opt.instances.append(inst)
            ev.options.append(opt)
        self._push(ev)

    def make_network_packet(self, player_uid: UID) -> NetworkPacketSessionEventArgs:
        pkt = NetworkPacketSessionEventArgs()
        pkt.session_id = self.session_id
        pkt.player_id = player_uid
        for ev in self.events:
            pkt.add_event(ev)
        if event_logger is not None:
            try:
                event_logger(self.session_id, player_uid,
                             [ev.to_byte_array() for ev in self.events])
            except Exception:
                pass
        self.events = []
        return pkt

    DELAY = 0

    def step(self) -> List[NetworkPacketSessionEventArgs]:
        packets = []

        if self.script_index >= len(self.script_commands):
            return packets

        cmd = self.script_commands[self.script_index]
        cmd_type = cmd.get("type", "")
        args = cmd.get("args", [])

        if cmd_type == "game_start":
            if self.player_champion_card_id is None:
                self.player_champion_card_id = self._new_card_id()
            if self.ai_champion_card_id is None:
                self.ai_champion_card_id = self._new_card_id()

            sleeve = "ab34d0b0-25e4-4afb-3ed1-30aceed4c69c"
            self.push_game_started()
            self.push_skip_setup()
            self.push_turn_phase(ETurnPhases.PreGame)
            self.push_disable_interface(True)
            self.push_deck_created(self.player_uid)
            self.push_deck_created(self.ai_uid)
            self.push_player_updated(self.player_uid, sleeve)
            self.push_player_updated(self.ai_uid, sleeve)

            packets.append(self.make_network_packet(self.player_uid))
            self.script_index += 1

        elif cmd_type == "showtip":
            lines = [a for a in args if isinstance(a, str)]
            for line in lines:
                self.push_show_tip(line)
            packets.append(self.make_network_packet(self.player_uid))
            self.script_index += 1

        elif cmd_type == "summon":
            name = args[0] if args else ""
            cdef_new = CardDef(
                name.lower(),
                ECardTypes.Troop if name.lower() == "baby yeti" else ECardTypes.Troop,
                0, 0, 1
            )
            cid = self._new_card_id()
            self.card_defs[cid] = cdef_new
            self.player_warzone.append(cid)

            self.push_card_updated(cid, self.player_uid, ECardCollections.Warzone, ECardTypes.Troop)
            self.push_card_moved(cid, self.player_uid, ECardCollections.Warzone)
            packets.append(self.make_network_packet(self.player_uid))
            self.script_index += 1

        elif cmd_type == "remove":
            names = [a for a in args if isinstance(a, str)]
            for name in names:
                name_lower = name.lower()
                for zone_list in [self.player_warzone, self.ai_warzone]:
                    for cid in list(zone_list):
                        cdef = self.card_defs.get(cid)
                        if cdef and cdef.name.lower() == name_lower:
                            zone_list.remove(cid)
                            self.push_card_moved(cid, self.player_uid, ECardCollections.Void)
            packets.append(self.make_network_packet(self.player_uid))
            self.script_index += 1

        elif cmd_type == "draw":
            count = args[0] if args and isinstance(args[0], int) else 1
            drawn = self.draw_cards(self.player_uid, count)
            for cid in drawn:
                cdef = self.card_defs.get(cid)
                if cdef and cdef.card_type == ECardTypes.Resource:
                    self.push_card_updated(cid, self.player_uid, ECardCollections.Hand, ECardTypes.Resource)
                else:
                    self.push_card_updated(cid, self.player_uid, ECardCollections.Hand, ECardTypes.Troop)
                self.push_card_moved(cid, self.player_uid, ECardCollections.Hand)
            packets.append(self.make_network_packet(self.player_uid))
            self.script_index += 1

        elif cmd_type == "disable_interface":
            enable = args[0] if args else True
            self.push_disable_interface(bool(enable))
            packets.append(self.make_network_packet(self.player_uid))
            self.script_index += 1

        elif cmd_type == "allow":
            names = [a for a in args if isinstance(a, str)]
            if not names or names[0] == "all":
                self.push_options(self.player_uid, list(self.player_hand))
            else:
                allowed = []
                for cid in self.player_hand:
                    cdef = self.card_defs.get(cid)
                    if cdef:
                        for n in names:
                            if cdef.name.lower() == n.lower():
                                allowed.append(cid)
                if allowed:
                    self.push_options(self.player_uid, allowed)
            self.push_green_light(self.player_uid, EPriorityContext.Normal)
            self.push_turn_phase(ETurnPhases.FirstMainPhase if self.current_phase < ETurnPhases.FirstMainPhase else self.current_phase)
            packets.append(self.make_network_packet(self.player_uid))
            self.script_index += 1

        elif cmd_type == "waitfor":
            self.script_index += 1

        elif cmd_type == "runto":
            self.script_index += 1

        elif cmd_type == "game_end":
            result = args[0] if args else "victory"
            ev = self._make_event(GameEndedSessionEventArgs)
            if result.lower() in ("defeat", "loss", "lose"):
                ev.winners = [self.ai_uid]
                ev.losers = [self.player_uid]
            else:
                ev.winners = [self.player_uid]
                ev.losers = [self.ai_uid]
            self._push(ev)
            packets.append(self.make_network_packet(self.player_uid))
            self.script_index += 1

        elif cmd_type == "phasestops":
            self.script_index += 1

        elif cmd_type == "setdeck":
            deck_side = args[0] if args else "player"
            deck_cards = args[1] if len(args) > 1 else []
            champ_name = args[2] if len(args) > 2 else "Mightsinger Alyndra"
            uid = self.player_uid if deck_side == "player" else self.ai_uid
            self.setup_deck(uid, deck_cards, champ_name)
            sleeve = "ab34d0b0-25e4-4afb-3ed1-30aceed4c69c"
            self.push_deck_created(uid)
            self.push_player_updated(uid, sleeve)
            packets.append(self.make_network_packet(uid))
            self.script_index += 1

        elif cmd_type == "skipsetup":
            self.push_skip_setup()
            self.script_index += 1

        elif cmd_type == "flush":
            self.script_index += 1

        elif cmd_type == "allow_pass":
            enable = args[0] if args else True
            if enable:
                self.push_green_light(self.player_uid, EPriorityContext.Normal)
            else:
                self.push_disable_interface(False)
            packets.append(self.make_network_packet(self.player_uid))
            self.script_index += 1

        else:
            log_msg = f"[tutorial] unknown command: {cmd_type}"
            print(log_msg)
            self.script_index += 1

        return packets


# ======================================================================
#  Script Parser
# ======================================================================

def parse_tutorial_script(filepath: str) -> List[dict]:
    """Parse tutorial.txt into a list of command dicts."""
    commands = []
    try:
        with open(filepath, 'r') as f:
            lines = f.readlines()
    except FileNotFoundError:
        return commands

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1

        if not line or line.startswith("//"):
            continue

        parts = line.split(None, 1)
        if not parts:
            continue
        cmd = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        if cmd == "setdeck":
            side = rest.split()[0] if rest else "player"
            deck_cards = []
            champ = ""
            full_text = rest
            while ";" not in full_text and i < len(lines):
                full_text += " " + lines[i].strip()
                i += 1
            champ_match = full_text.split("(champ ", 1)
            if len(champ_match) > 1:
                champ = champ_match[1].split(")", 1)[0].strip()
            card_entries = full_text.split("),")
            for entry in card_entries:
                entry = entry.strip().strip(";").strip()
                if entry.startswith("("):
                    card_name = entry[1:].strip()
                    if "champ " in card_name.lower():
                        continue
                    deck_cards.append(card_name)
            commands.append({"type": "setdeck", "args": [side, deck_cards, champ]})

        elif cmd == "draw":
            count_str = rest.strip("()")
            if count_str.lower() == "none":
                commands.append({"type": "draw", "args": [0]})
            else:
                try:
                    count = int(count_str)
                except ValueError:
                    count = 7
                commands.append({"type": "draw", "args": [count]})

        elif cmd == "waitfor":
            condition = rest.strip("()")
            if "hell to freeze" in condition.lower():
                commands.append({"type": "game_end"})
            else:
                commands.append({"type": "waitfor", "args": [condition]})

        elif cmd == "skipsetup":
            commands.append({"type": "skipsetup"})

        elif cmd == "allow":
            target = rest.strip("()")
            if "nothing" in target.lower():
                commands.append({"type": "allow", "args": []})
            elif "pass" in target.lower():
                pass_val = "false" not in target.lower()
                commands.append({"type": "allow", "args": ["pass", pass_val]})
            else:
                names = [n.strip() for n in target.split("),")]
                names = [n.strip("(") for n in names if n.strip("(").strip()]
                if names:
                    commands.append({"type": "allow", "args": names})

        elif cmd == "disable":
            val = rest.strip("()").strip().lower()
            enabled = not ("true" in val)
            commands.append({"type": "disable_interface", "args": [enabled]})

        elif cmd in ("showtip", "showhangingtip"):
            tips = []
            full_text = rest
            while ";" not in full_text and i < len(lines):
                full_text += " " + lines[i].strip()
                i += 1
            full_text = full_text.rstrip(";")
            tip_entries = [t.strip() for t in full_text.split("),")]
            for tip in tip_entries:
                tip = tip.strip("()")
                if tip:
                    parts2 = tip.split(None, 1)
                    if parts2 and parts2[0].isdigit():
                        if len(parts2) > 1:
                            tips.append(parts2[1])
                    else:
                        tips.append(tip)
            if tips:
                commands.append({"type": "showtip", "args": tips})

        elif cmd == "summon":
            card = rest.strip("()")
            commands.append({"type": "summon", "args": [card]})

        elif cmd == "remove":
            cards = [c.strip("()").strip() for c in rest.split("),")]
            cards = [c for c in cards if c]
            commands.append({"type": "remove", "args": cards})

        elif cmd == "runto":
            target = rest.strip("()")
            commands.append({"type": "runto", "args": [target]})

        elif cmd == "flush":
            commands.append({"type": "flush"})

        elif cmd == "phasestops":
            commands.append({"type": "phasestops"})

    return commands
