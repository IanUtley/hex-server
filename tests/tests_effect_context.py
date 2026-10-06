"""Focused tests for the context-style effect and ability-builder adapters."""

import os
import sys
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests.test_db import fresh_database

fresh_database()   # bind this process's database before ``db`` is imported

import game_engine
from rules_port.builder import AbilityBuilder
from abilities.framework.context import EffectContext
from abilities.framework.effects.registry import _LEAFS, effect
from gamedata.models import AbilityCost, TargetSpec
from gamedata.play_plan import (AbilityInstance, ActivationData, CardPlayCost,
                                PlayPlan)
from gamedata.semantics import AbilityGraph, EffectSpec


class _Cursor:
    rowcount = 0

    def fetchone(self):
        return (5, 7)


class _DB:
    def execute(self, _sql, _params):
        return _Cursor()

    def commit(self):
        pass


class _Session:
    session_id = 9


class _DrawHandler:
    def __init__(self):
        self.calls = []

    def _player_draw_card(self, game, session, draw_uid, owner):
        self.calls.append((game, session, draw_uid, owner))


def test_legacy_effect_context_rejects_rules_port_sessions():
    """Attached sessions cannot enter legacy leaf or child resolvers."""
    for session, bstate in (
            (SimpleNamespace(_rules_port_session=object()), {}),
            (_Session(), {"_rules_port_attached": True})):
        try:
            EffectContext.from_legacy(
                object(), session, _DB(), object(), "player", "ai",
                bstate, "legacy-effect")
        except RuntimeError as exc:
            assert "bypassed RulesPort" in str(exc), str(exc)
        else:
            raise AssertionError(
                "attached RulesPort session created a legacy context")

    # Direct construction must not bypass the factory boundary and enter a
    # legacy context on an attached session.
    attached = SimpleNamespace(_rules_port_session=object(), session_id=9)
    try:
        EffectContext(
            object(), attached, _DB(), object(), "player", "ai", {},
            "legacy-effect", param="00000000-0000-0000-0000-000000000001")
    except RuntimeError as exc:
        assert "bypassed RulesPort" in str(exc), str(exc)
    else:
        raise AssertionError("attached RulesPort created a legacy context")


class _Game:
    def __init__(self):
        self.events = []

    def _push(self, event):
        self.events.append(event)


class _ConversationHandler:
    def queue(self, game, session, player_uid, ai_uid, bstate, conversation_id):
        bstate["queued_conversation"] = conversation_id
        bstate["resolution_paused"] = True
        return "queued"


def test_native_grant_ability_can_target_a_champion():
    """Encounter setup grants persist on champions outside ``game_cards``."""
    player = game_engine.SessionCardId(game_engine.UID.make(244, 5))
    ai = game_engine.SessionCardId(game_engine.UID.make(3, 1000))
    handler = SimpleNamespace(
        user_profile={"id": 5}, _player_champ_scid=player,
        _ai_champ_scid=ai, _champion_granted_ability_guids={})
    granted = "ca145de3-61d9-d07e-51dd-fe51021e398d"
    context = EffectContext.from_rules_port(
        SimpleNamespace(events=[]), _Session(), object(), handler,
        game_engine.UID.make(244, 5), game_engine.UID.make(3, 1000),
        {"grant_target": player.uid.uid64}, "effect", granted)
    opponent_context = EffectContext.from_rules_port(
        SimpleNamespace(events=[]), _Session(), object(), handler,
        game_engine.UID.make(244, 5), game_engine.UID.make(3, 1000),
        {"grant_target": ai.uid.uid64}, "effect", granted)

    with mock.patch("pvp_db.db_card_grant_info", return_value=None), \
            mock.patch("pvp_db.db_ability_metadata_exists", return_value=True):
        result = context.grant_ability()
        opponent_result = opponent_context.grant_ability()

    assert result == f"granted 1 champion ability(s) to {hex(player.uid.uid64)}"
    assert opponent_result == f"granted 1 champion ability(s) to {hex(ai.uid.uid64)}"
    assert handler._champion_granted_ability_guids == {
        player.uid.uid64: [granted], ai.uid.uid64: [granted]}

    handler._champion_granted_ability_guids = {}
    game_started_context = EffectContext.from_rules_port(
        SimpleNamespace(events=[]), _Session(), object(), handler,
        game_engine.UID.make(244, 5), game_engine.UID.make(3, 1000),
        {"grant_target": player.uid.uid64, "event_type": "GameStartedEvent"},
        "effect", granted)
    with mock.patch("pvp_db.db_card_grant_info", return_value=None), \
            mock.patch("pvp_db.db_ability_metadata_exists", return_value=True), \
            mock.patch("rules_port.resolution.resolve_port_ability") as resolve:
        game_started_context.grant_ability()
    resolve.assert_called_once_with(
        handler, game_started_context.game, game_started_context.session,
        game_started_context.db, game_started_context.player_uid,
        game_started_context.ai_uid, game_started_context.bstate, granted,
        player.uid.uid64, 5, target_map={})


def test_player_target_template_uses_champion_identity_for_grants():
    """A PvE AI player's raw id 0 must resolve to its champion SessionCardId."""
    from rules_port.resolution import NativeEffectBackend

    ai = game_engine.SessionCardId(game_engine.UID.make(3, 1000))
    effect = SimpleNamespace(
        guid="grant-effect", concrete_type="GrantAbilityEffectTemplate",
        effect_group_id=1, target_index=0, effect_instance_id=0,
        contingent_effect_instance_id=-1, param="", condition_guid="")
    ability = SimpleNamespace(
        ability_template_id="grant-ability", source_uid=55,
        responsible_player_id=0, activation=SimpleNamespace(target_map={}),
        metadata=SimpleNamespace(targets=[SimpleNamespace(
            target_kind="PlayerTargetTemplate")]), ordered_effects=[effect])
    state = {}
    targets = []

    def native_effect(_kind, context, _effect):
        targets.append(context.bstate["grant_target"])
        return "granted"

    with mock.patch("rules_port.targeting.implicit_champion_target",
                    return_value=ai.uid.uid64):
        NativeEffectBackend()(
            handler=object(), game=SimpleNamespace(), session=_Session(),
            db=object(), player_uid=1, ai_uid=2, battle_state=state,
            ability=ability, native_effect=native_effect)

    assert targets == [ai.uid.uid64]


def test_native_discard_creates_parent_continuation_for_hand_picker():
    """A native discard target must pause the same typed ability instance.

    The continuation re-enters the paused effect, not the one after it: the
    first pass stopped before its mutation, so resuming past it discarded
    nothing at all.
    """
    state = {"resolving_effect_order": 3, "resolving_ability": "parent"}

    class Handler:
        def _push_discard_prompt(self, *args, **kwargs):
            args[4]["pending_discard_ability"] = kwargs.get(
                "ability_guid", "parent")
            return "prompted"

    class Ability:
        instance_id = 7

        @staticmethod
        def continuation(**kwargs):
            return {
                "ability_guid": "parent", "source_uid": 101,
                "owner_id": 5, "target_map": {}, "variables": {}, **kwargs,
            }

    context = EffectContext.from_rules_port(
        SimpleNamespace(events=[]), _Session(), None, Handler(), 1, 2,
        state, "effect", ability=Ability())
    assert context.discard() == "prompted"
    assert state["pending_discard_continuation"]["instance_id"] == 7
    assert state["pending_discard_continuation"]["resume_effect_order"] == 3
    assert state["resolution_paused"] is True


def test_context_decorator_adapts_legacy_leaf_arguments():
    seen = {}

    @effect("TestContextEffect")
    def capture(ctx: EffectContext):
        seen["target"] = ctx.target()
        seen["owner"] = ctx.target_owner()
        seen["value"] = ctx.value("m_InputValue", default=7)
        return "captured"

    result = _LEAFS["TestContextEffect"](
        object(), _Session(), _DB(), object(), "player", "ai",
        {"resolving_target_uid": 123}, "missing-effect", "")

    assert result == "captured"
    assert seen == {"target": 123, "owner": 5, "value": 7}

    # Direct imports retain the historical ABI while the resolver uses the
    # same registered adapter.
    assert capture(
        object(), _Session(), _DB(), object(), "player", "ai",
        {"resolving_target_uid": 123}, "missing-effect", "") == "captured"


def test_tunnel_moves_source_to_underground_and_emits_zone_triggers():
    class TunnelDB:
        def __init__(self):
            self.sql = []

        def execute(self, sql, params):
            self.sql.append((sql, params))
            if sql.startswith("SELECT user_id, location"):
                return type("Cursor", (), {
                    "fetchone": lambda self: (5, "warzone")})()
            return type("Cursor", (), {
                "fetchone": lambda self: None})()

        def commit(self):
            pass

    db = TunnelDB()
    game = _Game()
    context = EffectContext.from_rules_port(
        game, _Session(), db, object(), "player", "ai",
        # Legacy target aliases may be left behind by a nested trigger. A
        # source-bound Tunnel effect must continue to use the current typed
        # source instead of moving one of those stale cards.
        {"resolving_source_uid": 123, "resolving_target_uid": None,
         "player_spell_target": 999, "player_mod_target": 998},
        "effect", "")
    with mock.patch("rules_port.zone_effects.project_card"), \
            mock.patch("rules_port.triggers.dispatch_trigger") as triggers, \
            mock.patch(
                "rules_port.creation_effects.activate_creation_replacements"
            ) as replacements:
        assert context.tunnel() == "tunneled 0x7b"
    # Reese's replacement is granted by Underground -> CastSpells, not by
    # entering Underground. It must remain inactive while the card is buried.
    replacements.assert_not_called()
    update = next(sql for sql, _params in db.sql
                  if sql.startswith("UPDATE game_cards"))
    assert "location='underground'" in update
    update_params = next(params for sql, params in db.sql
                         if sql.startswith("UPDATE game_cards"))
    assert int(update_params[-1]) == 123
    assert triggers.call_count == 2
    assert [call.args[1] for call in triggers.call_args_list] == [
        "CardExitedZoneEvent", "CardEnteredZoneEvent"]


def test_surface_queues_the_buried_card_for_free_with_speed():
    """Surface uses the shared free-play projection, not a Records graph."""
    from rules_port.tunneling import resolve_surface

    context = SimpleNamespace(
        db=object(), session=_Session(), handler=object(), game=object(),
        player_uid="player", ai_uid="ai", bstate={})
    with mock.patch(
            "rules_port.tunneling.surface_source_is_underground",
            return_value=True), \
            mock.patch("pvp_db.db_card_chain_info",
                       return_value=("spy-template", "Troop", 5)), \
            mock.patch("pvp_db.db_card_owner_zone_state",
                       return_value=(5, "underground", 0)), \
            mock.patch("rules_port.host_mutations.queue_free_played_card",
                       return_value="queued troop 123 for free") as queue:
        assert resolve_surface(context, 123) == "queued troop 123 for free"

    queue.assert_called_once_with(
        context.handler, context.game, context.session, context.db,
        context.player_uid, context.ai_uid, context.bstate, 123, 5,
        "spy-template", "Troop")


def test_current_resource_modifier_persists_before_next_rules_port_cost():
    """A temporary resource grant must update all three state projections.

    Hideous Conversion grants current resource, not maximum resource.  The
    client receives the pool event immediately, so the checkpoint must be
    saved at the same effect boundary or the next RulesPort activation can
    incorrectly charge from the old pool.
    """
    from abilities.framework.bom import _apply_resource_property
    import battle_engine

    class Handler:
        user_profile = {"id": 5}

    game = _Game()
    game.player_uid = "player"
    game.ai_uid = "ai"
    session = _Session()
    state = {
        "player_resources": 1,
        "player_total_resources": 3,
        "ai_resources": 0,
        "ai_total_resources": 0,
    }
    with mock.patch.object(battle_engine, "save_state") as save_state:
        result = _apply_resource_property(
            game, session, _DB(), Handler(), "player", "ai", state,
            {"property": "currentresource", "amount": 1}, None)

    assert result == "player resources 1->2"
    assert state["player_resources"] == 2
    assert game.player_resources == 2
    assert game.events[-1].new_value == 2
    save_state.assert_called_once_with(session, state)


def test_tunneling_metadata_and_underground_visibility_are_data_driven():
    from abilities.framework.effects.counters import counter_is_secret, tunneling_value
    from domain.game import Game
    from domain.types import UID, SessionCardId
    import game_engine

    # Minion of Yazukan's Tunneling value is a CardTemplate TAC IntAttr, not
    # a name/text rule in the server.
    assert tunneling_value(
        None, "4cd28b0f-a50e-4bd4-b07d-cd2265e0a403") == 2
    assert tunneling_value(
        None, "4cd28b0f-a50e-4bd4-b07d-cd2265e0a403",
        {"Tunneling": 1}) == 1

    owner = UID.make(244, 1001)
    opponent = UID.make(244, 1002)
    card = SessionCardId(UID(0x6501))
    template = "4cd28b0f-a50e-4bd4-b07d-cd2265e0a403"
    counter = "def75520-0b8b-447f-8705-b34e71043890"
    assert counter_is_secret(counter) is False

    class Packet:
        def __init__(self):
            self.events = []

        def add_event(self, event):
            self.events.append(event)

    def packet_for(viewer):
        game = Game(1, owner, opponent)
        game.push_card_updated(
            card, owner, game_engine.ECardCollections.Underground,
            game_engine.ECardTypes.Troop, template_id=template,
            counters={counter: 1})
        with mock.patch("domain.game.NetworkPacketSessionEventArgs", Packet):
            return game.make_network_packet(viewer).events[0]

    own = packet_for(owner)
    other = packet_for(opponent)
    assert own.nulling is False
    assert other.nulling is True
    assert other.card_id.guid.int == 0
    assert [str(value.guid) for value in other.counter_templates] == [counter]
    assert list(other.counter_counts) == [1]


def test_hidden_card_update_sends_no_template_identity_or_stats():
    from domain.game import Game
    from domain.types import UID, SessionCardId
    import game_engine

    owner = UID.make(244, 1001)
    opponent = UID.make(244, 1002)
    card = SessionCardId(UID(0x6602))
    template = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

    class Packet:
        def __init__(self):
            self.events = []

        def add_event(self, event):
            self.events.append(event)

    game = Game(1, owner, opponent)
    game.push_card_updated(
        card, opponent, game_engine.ECardCollections.Hand,
        game_engine.ECardTypes.Troop, template_id=template,
        attack=5, defense=4, cost=3, nulling=True)
    original = game.events[-1]
    original.orig_template = game_engine.ResourceId.from_str(template)
    original.int_attrs = {"PrivateAbilityValue": 7}
    with mock.patch("domain.game.NetworkPacketSessionEventArgs", Packet):
        packet = game.make_network_packet(owner)

    hidden = packet.events[0]
    assert hidden.nulling is True
    assert hidden.session_card_id == card
    assert hidden.collection == game_engine.ECardCollections.Hand
    assert hidden.card_id.guid.int == 0
    assert hidden.orig_template.guid.int == 0
    assert hidden.attack == hidden.defense == hidden.cost == 0
    assert hidden.int_attrs == {}
    # Viewer filtering must operate on a clone; the controller's projection
    # retains the complete card definition for a later authorized packet.
    assert original.card_id.guid.int != 0
    assert original.attack == 5


def test_other_players_choosing_card_is_hidden_even_without_nulling_flag():
    from domain.game import Game
    from domain.types import UID, SessionCardId
    import game_engine

    owner = UID.make(244, 1001)
    opponent = UID.make(244, 1002)
    card = SessionCardId(UID(0x6603))
    template = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

    class Packet:
        def __init__(self):
            self.events = []

        def add_event(self, event):
            self.events.append(event)

    def packet_for(viewer):
        game = Game(1, owner, opponent)
        game.push_card_updated(
            card, opponent, game_engine.ECardCollections.Choosing,
            game_engine.ECardTypes.Choice, template_id=template,
            attack=2, nulling=False)
        with mock.patch("domain.game.NetworkPacketSessionEventArgs", Packet):
            return game.make_network_packet(viewer).events[0]

    opponent_view = packet_for(owner)
    owner_view = packet_for(opponent)

    assert opponent_view.nulling is True
    assert opponent_view.card_id.guid.int == 0
    assert opponent_view.attack == 0
    assert owner_view.nulling is False
    assert str(owner_view.card_id.guid) == template


def test_packet_visibility_matches_csharp_collections_and_reveal_scopes():
    from domain.game import Game
    from domain.types import UID, SessionCardId
    import game_engine

    owner = UID.make(244, 1001)
    opponent = UID.make(244, 1002)
    card = SessionCardId(UID(0x6604))
    template = "cccccccc-cccc-cccc-cccc-cccccccccccc"

    class Packet:
        def __init__(self):
            self.events = []

        def add_event(self, event):
            self.events.append(event)

    def packet_for(viewer, controller, collection, *, nulling=False,
                   visibility=None, tag=None, chain=False, card_type=None,
                   tunneling=0, counters=None,
                   append_hidden_placeholder=False):
        game = Game(1, owner, opponent)
        if visibility:
            game._visibility_by_uid[int(viewer.uid64)] = visibility
        game.push_card_updated(
            card, controller, collection,
            card_type or game_engine.ECardTypes.Troop,
            template_id=template, attack=4, defense=3, cost=2,
            nulling=nulling, tunneling=tunneling, counters=counters)
        if tag is not None:
            game.events[-1]._hand_reveal_viewer_uid = tag
        if chain:
            game.events[-1]._chain_reveal = True
        if append_hidden_placeholder:
            game.push_card_updated(
                card, controller, collection, game_engine.ECardTypes.Troop,
                nulling=True)
        with mock.patch("domain.game.NetworkPacketSessionEventArgs", Packet):
            return game.make_network_packet(viewer).events

    hidden_cases = (
        # Deck remains face down even for its controller without a scoped
        # top-of-deck reveal.
        (owner, owner, game_engine.ECardCollections.Deck),
        (owner, opponent, game_engine.ECardCollections.Hand),
        (owner, opponent, game_engine.ECardCollections.Underground),
        (owner, opponent, game_engine.ECardCollections.Choosing),
        (owner, opponent, game_engine.ECardCollections.Hand |
         game_engine.ECardCollections.Choosing),
        (owner, owner, game_engine.ECardCollections.Mod),
    )
    for viewer, controller, collection in hidden_cases:
        public_tunneling_counter = (
            {"def75520-0b8b-447f-8705-b34e71043890": 1}
            if collection == game_engine.ECardCollections.Underground else None)
        projected = packet_for(
            viewer, controller, collection,
            counters=public_tunneling_counter)[0]
        assert projected.nulling is True, collection
        assert projected.card_id.guid.int == 0, collection
        assert projected.attack == projected.defense == projected.cost == 0
        if collection == game_engine.ECardCollections.Underground:
            assert [str(value.guid) for value in projected.counter_templates] == [
                "def75520-0b8b-447f-8705-b34e71043890"]
            assert list(projected.counter_counts) == [1]

    visible_cases = (
        (owner, owner, game_engine.ECardCollections.Hand),
        (owner, owner, game_engine.ECardCollections.Underground),
        (owner, owner, game_engine.ECardCollections.Choosing),
        (owner, owner, game_engine.ECardCollections.Hand |
         game_engine.ECardCollections.Choosing),
        (owner, opponent, game_engine.ECardCollections.None_),
        (owner, opponent, game_engine.ECardCollections.Warzone),
        (owner, opponent, game_engine.ECardCollections.Discard),
        (owner, opponent, game_engine.ECardCollections.CastSpells),
    )
    for viewer, controller, collection in visible_cases:
        projected = packet_for(viewer, controller, collection)[0]
        assert projected.nulling is False, collection
        assert str(projected.card_id.guid) == template, collection

    hand_permission = packet_for(
        owner, opponent, game_engine.ECardCollections.Hand, nulling=True,
        visibility={"CanSeeOpponentsHand": 1})[0]
    assert hand_permission.nulling is False
    assert str(hand_permission.card_id.guid) == template

    underground_permission = packet_for(
        owner, opponent, game_engine.ECardCollections.Underground,
        visibility={"CanSeeUndergroundTroops": 1})[0]
    assert underground_permission.nulling is False
    assert str(underground_permission.card_id.guid) == template

    champion_in_deck = packet_for(
        owner, opponent, game_engine.ECardCollections.Deck,
        card_type=game_engine.ECardTypes.Champion)[0]
    assert champion_in_deck.nulling is False
    assert str(champion_in_deck.card_id.guid) == template

    # Existing shared refreshes can contain an unscoped full Deck update.
    # The authored top-deck projector must append a separately scoped copy so
    # the authorized viewer sees it and the other client still gets a nulling
    # representation.
    from rules_port.visibility import project_visible_top_decks
    top_game = Game(1, owner, opponent)
    top_game.push_card_updated(
        card, owner, game_engine.ECardCollections.Deck,
        game_engine.ECardTypes.Troop, template_id=template)
    top_handler = SimpleNamespace(_card_full_data=lambda *_args: (
        template, game_engine.ECardTypes.Troop, "Card", 0, 0, 1, 0))
    top_state = {"pvp": True, "pids": [1001, 1002]}

    def top_attributes(_db, _session_id, _state, player):
        return {"CanSeeTopOfDeck": int(player) == 1001}

    with mock.patch("pvp_db.db_deck_top_card_details",
                    return_value=(1, card.uid.uid64, template, template)), \
            mock.patch("rules_port.static_rules.player_int_attributes",
                       side_effect=top_attributes):
        project_visible_top_decks(
            object(), SimpleNamespace(session_id=1), top_handler, top_game,
            owner, opponent, top_state)
    top_updates = list(top_game.events)
    with mock.patch("domain.game.NetworkPacketSessionEventArgs", Packet):
        top_owner_events = top_game.make_network_packet(owner).events
        # make_network_packet consumes its event queue; give the second
        # recipient the same pre-projection batch, as the service adapters do.
        top_game.events = list(top_updates)
        top_opponent_events = top_game.make_network_packet(opponent).events
    assert len(top_owner_events) == 1
    assert top_owner_events[0].nulling is False
    assert str(top_owner_events[0].card_id.guid) == template
    assert len(top_opponent_events) == 1, [
        (event.collection, event.nulling, event.card_id.guid.int)
        for event in top_opponent_events]
    assert top_opponent_events[0].nulling is True
    assert top_opponent_events[0].card_id.guid.int == 0

    # A public chain reveal is allowed for both players, but Underground has
    # an explicit C# exception and remains private even with the same marker.
    for viewer in (owner, opponent):
        chain_card = packet_for(
            viewer, opponent, game_engine.ECardCollections.Hand,
            chain=True, append_hidden_placeholder=True)
        assert len(chain_card) == 1
        assert chain_card[0].nulling is False
        assert str(chain_card[0].card_id.guid) == template
    tunneled_chain = packet_for(
        owner, opponent, game_engine.ECardCollections.Underground,
        chain=True)[0]
    assert tunneled_chain.nulling is True
    assert tunneled_chain.card_id.guid.int == 0
    nonzero_tunneling_chain = packet_for(
        owner, opponent, game_engine.ECardCollections.Hand,
        chain=True, tunneling=1)[0]
    assert nonzero_tunneling_chain.nulling is True
    assert nonzero_tunneling_chain.card_id.guid.int == 0

    # Reconnect must restore the hidden zone to the owner, hide it from an
    # unauthorized opponent, and send one full projection to an authorized
    # opponent. The full reveal suppresses that viewer's hidden placeholder.
    import services.tournament_game as tournament_game
    from rules_port.visibility import (
        apply_player_visibility_to_game,
        merge_underground_visibility_flags)
    session = SimpleNamespace(session_id=1)
    underground_row = (card.uid.uid64, template, 1002, "Troop", 0, "[]", "{}")
    visible_to = {1001: 0}

    def underground_rows(_session_id, location, **_kwargs):
        return [underground_row] if location == "underground" else []

    def projected_underground_rows(_session_id, user_id, **_kwargs):
        return [(card.uid.uid64, template)] if int(user_id) == 1002 else []

    def visibility_attrs(_db, _session_id, _state, player):
        return {"CanSeeUndergroundTroops": visible_to.get(int(player), 0)}

    handler = SimpleNamespace(_card_full_data=lambda *_args: (
        template, game_engine.ECardTypes.Troop, "Card", 0, 0, 1, 0))

    def reconnect_underground_packet(viewer, can_see):
        visible_to[1001] = int(can_see)
        state = {"pvp": True, "pids": [1001, 1002],
                 "player_visibility": ({"1001": {
                     "CanSeeUndergroundTroops": 1}} if can_see else {})}
        game = Game(1, owner, opponent)
        with mock.patch.object(
                tournament_game, "db_game_cards_at_location",
                side_effect=underground_rows), \
                mock.patch(
                    "pvp_db.db_underground_card_rows",
                    side_effect=projected_underground_rows), \
                mock.patch(
                    "rules_port.static_rules.player_int_attributes",
                    side_effect=visibility_attrs):
            state["player_visibility"] = merge_underground_visibility_flags(
                object(), session, handler, state,
                state.get("player_visibility"), owners=(1001, 1002))
            apply_player_visibility_to_game(game, state)
            tournament_game._pvp_push_reconnect_underground(
                game, session, handler, int(viewer.uid64) >> 8,
                owner, opponent, state)
        with mock.patch("domain.game.NetworkPacketSessionEventArgs", Packet):
            return game.make_network_packet(viewer).events

    visible_reconnect = reconnect_underground_packet(owner, True)
    assert len(visible_reconnect) == 1
    assert visible_reconnect[0].nulling is False
    assert str(visible_reconnect[0].card_id.guid) == template
    hidden_reconnect = reconnect_underground_packet(owner, False)
    assert len(hidden_reconnect) == 1
    assert hidden_reconnect[0].nulling is True
    assert hidden_reconnect[0].card_id.guid.int == 0
    owner_reconnect = reconnect_underground_packet(opponent, False)
    assert len(owner_reconnect) == 1
    assert owner_reconnect[0].nulling is False
    assert str(owner_reconnect[0].card_id.guid) == template

    # A viewer-scoped reveal must not add its UID to another client's public
    # chain set or suppress that client's hidden placeholder.
    scoped_owner = packet_for(
        owner, opponent, game_engine.ECardCollections.Hand,
        tag=owner, chain=True, append_hidden_placeholder=True)
    scoped_opponent = packet_for(
        opponent, opponent, game_engine.ECardCollections.Hand,
        tag=owner, chain=True, append_hidden_placeholder=True)
    assert len(scoped_owner) == 1
    assert scoped_owner[0].nulling is False
    assert str(scoped_owner[0].card_id.guid) == template
    assert len(scoped_opponent) == 1
    assert scoped_opponent[0].nulling is True
    assert scoped_opponent[0].card_id.guid.int == 0


def test_subterranean_spy_visibility_reveals_only_the_controller_view():
    from domain.game import Game
    from domain.types import UID, SessionCardId
    import game_engine

    owner = UID.make(244, 1001)
    opponent = UID.make(244, 1002)
    opponent_card = SessionCardId(UID(0x6601))

    class Packet:
        def __init__(self):
            self.events = []

        def add_event(self, event):
            self.events.append(event)

    game = Game(1, owner, opponent)
    game._visibility_by_uid[int(owner.uid64)] = {
        "CanSeeOpponentsHand": 1,
    }
    game.push_card_updated(
        opponent_card, opponent, game_engine.ECardCollections.Hand,
        game_engine.ECardTypes.Troop,
        template_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", nulling=True)
    with mock.patch("domain.game.NetworkPacketSessionEventArgs", Packet):
        owner_packet = game.make_network_packet(owner)
    assert owner_packet.events[0].nulling is False

    game = Game(1, owner, opponent)
    game._visibility_by_uid[int(owner.uid64)] = {
        "CanSeeOpponentsHand": 1,
    }
    game.push_card_updated(
        opponent_card, opponent, game_engine.ECardCollections.Hand,
        game_engine.ECardTypes.Troop,
        template_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", nulling=True)
    with mock.patch("domain.game.NetworkPacketSessionEventArgs", Packet):
        opponent_packet = game.make_network_packet(opponent)
    assert opponent_packet.events[0].nulling is True

    # A phase checkpoint can contain both the full Spy projection and the
    # normal opponent-hand placeholder.  The placeholder has an invalid
    # template id; if it is allowed to overwrite the full event, Unity renders
    # a visible but black rectangle.  Keep the complete definition as the
    # only event for this card/viewer.
    game = Game(1, owner, opponent)
    game._visibility_by_uid[int(owner.uid64)] = {
        "CanSeeOpponentsHand": 1,
    }
    game.push_card_updated(
        opponent_card, opponent, game_engine.ECardCollections.Hand,
        game_engine.ECardTypes.Troop,
        template_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", nulling=False)
    game.events[-1]._hand_reveal_viewer_uid = owner
    game.push_card_updated(
        opponent_card, opponent, game_engine.ECardCollections.Hand,
        game_engine.ECardTypes.Troop, nulling=True)
    with mock.patch("domain.game.NetworkPacketSessionEventArgs", Packet):
        projected_packet = game.make_network_packet(owner)
    assert len(projected_packet.events) == 1
    assert projected_packet.events[0].nulling is False
    assert str(projected_packet.events[0].card_id.guid) == \
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def test_tunneling_surface_queue_is_empty_without_underground_cards():
    from abilities.framework.effects.counters import (
        queue_tunneling_surfaces, surface_source_is_underground)

    class EmptyCursor:
        def fetchall(self):
            return []

        def fetchone(self):
            return None

    class EmptyDB:
        def execute(self, _sql, _params):
            return EmptyCursor()

    class Session:
        session_id = 1

    class Handler:
        pass

    class Game:
        events = []

    bstate = {"stack": []}
    assert not surface_source_is_underground(EmptyDB(), Session(), 123)
    assert queue_tunneling_surfaces(
        EmptyDB(), Session(), Handler(), Game(), "player", "ai", bstate,
        1001) == []
    assert bstate["stack"] == []


def test_queue_surfaces_registers_the_native_chain_item():
    """A thresholded underground card must reach the native scheduler.

    ``rules_port.tunneling.queue_surfaces`` spent the counter and pushed only
    the wire-compatible descriptor, so the Surface stayed on the client's
    chain with no native item to resolve: the buried card never surfaced and
    could not be re-queued because the orphan kept the stack non-empty.
    """
    import json as _json
    from rules_port.actions import ResolveTopOfChainAction
    from rules_port.kernel import PriorityWindowAction
    from rules_port import tunneling
    from rules_port.context import EffectContext
    from rules_port.session import AuthoritativeSession
    from tests.tests_combat import (make_db, add_card, HandlerStub,
                                    SessionStub)

    db = make_db()
    try:
        template = "aaaaaaaa-0000-0000-0000-0000000000aa"
        db.execute(
            "INSERT INTO card_templates "
            "(guid,name,card_type,cost,attack,defense,attributes,"
            "abilities_json,threshold_json,subtype) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (template, "Wakizashi Ambusher", "Troop", 4, 2, 1, 0, "[]", "[]", ""))
        uid = game_engine.UID.make(1, 8).uid64
        add_card(db, uid, 5, template, loc="underground")
        db.execute(
            "UPDATE game_cards SET permanent_buffs=? WHERE card_uid=?",
            (_json.dumps({"counters": {"tunneling": 3}}), uid))
        db.commit()

        pl_t = game_engine.UID.make(244, 5)
        ai_t = game_engine.UID.make(3, 1000)
        game = game_engine.Game(1, pl_t, ai_t)
        port = AuthoritativeSession(1, (pl_t, ai_t), seed_z=1, seed_w=2)
        session = SessionStub()
        session._rules_port_session = port
        bstate = {"stack": []}
        context = EffectContext.from_rules_port(
            game, session, db, HandlerStub(db), pl_t, ai_t, bstate, "",
            ability=None)
        with mock.patch.object(tunneling, "tunneling_value", return_value=3):
            queued = tunneling.queue_surfaces(context, 5)

        assert queued == [int(uid)], queued
        wire = bstate["stack"][-1]
        assert wire["ability_guid"] == tunneling.SURFACE_ABILITY_GUID, wire
        assert wire["source_uid"] == int(uid), wire
        instance_id = int(wire["instance_id"])
        assert port.chain._instance_ids == [instance_id], port.chain._instance_ids
        assert port._projected_chain_descriptors[instance_id]["instance_id"] == \
            instance_id, port._projected_chain_descriptors
        top = port.action_stack.peek()
        assert isinstance(top, PriorityWindowAction), top
        assert int(getattr(top.ability_responding_to, "instance_id", -1)) == \
            instance_id, top.ability_responding_to
        assert port.action_stack.priority_player_id == pl_t, \
            port.action_stack.priority_player_id
        assert any(isinstance(action, ResolveTopOfChainAction)
                   for action in port.action_stack._stack), port.action_stack._stack
        assert _json.loads(db.execute(
            "SELECT permanent_buffs FROM game_cards WHERE card_uid=?",
            (uid,)).fetchone()[0]).get("counters") == {}, "counter not spent"
    finally:
        db.close()


def test_resource_choice_ability_is_detected_from_metadata():
    from abilities.framework.resources import printed_resource_choice_ability

    assert printed_resource_choice_ability([
        "8dcbdee1-e1de-1acd-5b17-e8beaec808b4",
        "43461713-afe7-299f-9aba-40002e9105eb",
    ]) == "8dcbdee1-e1de-1acd-5b17-e8beaec808b4"
    assert printed_resource_choice_ability([
        "43461713-afe7-299f-9aba-40002e9105eb",
    ]) is None


def test_match_secondary_reveal_uses_stored_target_owner():
    from abilities.framework.bom import _reveal_owner_for_target

    class OwnerCursor:
        def fetchone(self):
            return (1002,)

    class OwnerDB:
        def execute(self, _sql, _params):
            return OwnerCursor()

    target_champion = 0x6801
    battle_state = {
        "pvp": True,
        "stored_targets": {"withering-touch": [target_champion]},
    }
    owner = _reveal_owner_for_target(
        OwnerDB(), object(), _Session(), battle_state, 1001,
        "MatchSecondaryTargetTemplate", "withering-touch")
    assert owner == 1002


def test_player_updated_carries_subterranean_spy_hand_permission():
    from domain.game import Game
    from domain.types import UID, SessionCardId
    import game_engine

    owner = UID.make(244, 1001)
    opponent = UID.make(244, 1002)
    game = Game(1, owner, opponent)
    champion = SessionCardId(UID(0x6701))
    game.player_champion_card_id = champion
    game._visibility_by_uid[int(owner.uid64)] = {
        "CanSeeOpponentsHand": 1,
    }
    game.push_player_updated(owner, champ_id=champion)
    event = game.events[-1]
    assert event.can_see_enemy_hand is True


def test_visibility_refresh_projects_underground_permission_to_unity_flag():
    from domain.game import Game
    from domain.types import UID
    from rules_port.visibility import refresh_player_visibility

    player = UID.make(244, 1001)
    opponent = UID.make(244, 1002)
    game = Game(1, player, opponent)
    player_champion = game_engine.SessionCardId(UID(0x6501))
    opponent_champion = game_engine.SessionCardId(UID(0x6502))
    game.player_champion_card_id = player_champion
    game.ai_champion_card_id = opponent_champion
    session = SimpleNamespace(session_id=1)
    state = {"pvp": True, "pids": [1001, 1002],
             "champ_map": {"1001": 0x6501, "1002": 0x6502},
             "player_visibility": {}}
    allowed = {1001: 1, 1002: 0}
    player_attrs = lambda _db, _sid, _state, owner: {
        "CanSeeUndergroundTroops": allowed[int(owner)]}
    handler = SimpleNamespace(user_profile={})
    empty_rows = mock.patch(
        "pvp_db.db_visibility_underground_rows", return_value=[])
    empty_hands = mock.patch("pvp_db.db_hand_card_uids", return_value=[])
    empty_underground = mock.patch(
        "pvp_db.db_underground_card_rows", return_value=[])
    attrs_patch = mock.patch(
        "rules_port.static_rules.player_int_attributes",
        side_effect=player_attrs)
    with empty_rows, empty_hands, empty_underground, attrs_patch:
        refresh_player_visibility(
            object(), session, handler, game, player, opponent, state)
        assert state["player_visibility"]["1001"] == {
            "CanSeeUndergroundTroops": 1}
        assert game.events[-1].player_id == player
        assert game.events[-1].can_see_enemy_underground is True

        allowed[1001] = 0
        refresh_player_visibility(
            object(), session, handler, game, player, opponent, state)
        assert "1001" not in state["player_visibility"]
        assert game.events[-1].player_id == player
        assert game.events[-1].can_see_enemy_underground is False


def test_player_updated_preserves_the_established_deck_sleeve():
    """The client assigns DeckSleeveId unconditionally from PlayerUpdated.

    Any update that omits the sleeve (every draw/health/resource refresh)
    reverted the opponent's cards to the default Hex sleeve once another
    player state change arrived.  GameStarted/DeckCreated establish it once and
    later updates must keep carrying it.
    """
    from domain.game import Game
    from domain.types import UID, SessionCardId

    owner = UID.make(244, 1001)
    opponent = UID.make(244, 1002)
    game = Game(1, owner, opponent)
    sleeve = "7b05554f-4ced-4096-931d-792a7c529650"
    champion = SessionCardId(UID(0x6702))
    game.ai_champion_card_id = champion
    game.push_game_started(
        champion_names=["Player", "Opponent"],
        champion_template_ids=["1d462ffb-0744-4996-804c-ba61b2c5c2f1",
                               "f8f86969-2e47-4901-8c9e-7fbf8d859e22"],
        sleeve_template_ids=[
            "c508cdd3-77ad-4dbf-b1b4-b201eae5a690", sleeve])
    game.events.clear()
    game.push_player_updated(opponent, champ_id=champion)
    event = game.events[-1]
    assert str(event.deck_sleeve_id.guid) == sleeve, event.deck_sleeve_id


def test_effect_context_keeps_runtime_builder_out_of_battle_state():
    import json

    builder = object()
    state = {"turn_player": "player"}
    context = EffectContext.from_rules_port(
        object(), _Session(), _DB(), object(), "player", "ai", state,
        "effect", "", ability=builder)
    assert context.ability is builder
    assert "_ability_builder" not in state
    assert json.dumps(state) == '{"turn_player": "player"}'


def test_continuation_carries_only_serializable_activation_data():
    from rules_port.builder import AbilityContinuation

    pending = AbilityContinuation.from_state({
        "resolving_ability": "ABILITY-GUID",
        "resolving_source_uid": 123,
        "resolving_owner_id": 5,
        "resolving_effect_order": 4,
        "ability_target_map": {0: [456]},
        "ability_variables": {"Roll": 2},
    }).to_dict()
    assert pending == {
        "ability_guid": "ability-guid", "source_uid": 123,
        "owner_id": 5, "target_map": {"0": [456]},
        "variables": {"Roll": 2}, "resume_effect_order": 5,
    }
    import json
    assert json.loads(json.dumps(pending)) == pending


def test_context_activate_ability_uses_typed_child_resolver():
    import abilities.framework.resolution as resolution

    state = {
        "resolving_source_uid": 123,
        "resolving_owner_id": 5,
        "ability_variables": {"Roll": 2},
    }
    context = EffectContext.from_rules_port(
        object(), _Session(), _DB(), object(), "player", "ai", state,
        "effect", "child-guid")
    with mock.patch.object(resolution, "resolve_ability", return_value="child") as run:
        assert context.activate_ability() == "child"
    assert run.call_args.args[7:10] == ("child-guid", 123, 5)
    assert run.call_args.kwargs["target_map"] == {}
    assert run.call_args.kwargs["variables"] == {"Roll": 2}


def test_ability_trace_records_authoritative_zone_and_event_deltas():
    from abilities.framework.trace import _CARD_COLUMNS, begin_effect, end_effect

    class TraceDB:
        def __init__(self):
            self.rows = {
                101: {"user_id": 5, "location": "deck", "position": 0},
                102: {"user_id": 5, "location": "deck", "position": 1},
            }

        def execute(self, _sql, _params):
            return type("Cursor", (), {
                "fetchall": lambda cursor: [
                    (uid,) + tuple(row.get(column) for column in _CARD_COLUMNS)
                    for uid, row in self.rows.items()],
            })()

    db = TraceDB()
    game = _Game()
    state = {"trace_ability_resolution": True}
    effect = {
        "effect_guid": "bury-effect",
        "effect_type": "BuryCardAbilityEffectTemplate",
        "effect_order": 2,
    }
    started = begin_effect(db, _Session(), game, state, effect, 123)
    db.rows[101]["location"] = "discard"
    db.rows[101]["card_cost_mod"] = -2
    db.rows[101]["card_attributes"] = 16
    state["player_health"] = 23
    state["player_charges"] = 4
    state["player_resources"] = 3
    state["player_total_resources"] = 5
    state["player_spell_points"] = 2
    state["player_threshold"] = {4: 2}
    game.events.append(type("CardMovedSessionEventArgs", (), {})())
    end_effect(db, _Session(), game, state, started, result="buried 1")

    trace = state["ability_trace"]
    assert trace[0]["effect_type"] == "BuryCardAbilityEffectTemplate"
    assert trace[0]["events"] == [{
        "type": "CardMovedSessionEventArgs", "fields": {},
    }]
    change = trace[0]["card_changes"]
    assert len(change) == 1 and change[0]["card_uid"] == 101
    assert change[0]["before"]["location"] == "deck"
    assert change[0]["after"]["location"] == "discard"
    assert change[0]["after"]["card_cost_mod"] == -2
    assert change[0]["after"]["card_attributes"] == 16
    assert trace[0]["champion_changes"] == {
        "player_charges": {"before": None, "after": 4},
        "player_health": {"before": None, "after": 23},
        "player_resources": {"before": None, "after": 3},
        "player_spell_points": {"before": None, "after": 2},
        "player_threshold": {"before": None, "after": {"4": 2}},
        "player_total_resources": {"before": None, "after": 5},
    }


def test_extra_combat_entries_extend_the_current_turn_after_second_main():
    import battle_engine
    import game_engine

    state = {
        "player_has_ready_troop": False,
        "extra_combats_this_turn": {
            "player": [{"ready_your_troops": True}],
        },
    }
    phases = battle_engine.build_turn_phases(state)
    second_main = phases.index(game_engine.ETurnPhases.SecondMainPhase)
    assert phases[second_main + 1:second_main + 1 + len(
        battle_engine.COMBAT_STEPS)] == battle_engine.COMBAT_STEPS
    assert phases[second_main + 1 + len(battle_engine.COMBAT_STEPS)] == \
        game_engine.ETurnPhases.SecondMainPhase


def test_pvp_extra_combat_entries_use_the_active_player_key():
    from services.tournament_game import _pvp_turn_phase_list
    import battle_engine
    import game_engine

    phases = _pvp_turn_phase_list(
        {"extra_combats_this_turn": {"1001": [{}]}}, 1001, False)
    second_main = phases.index(game_engine.ETurnPhases.SecondMainPhase)
    assert phases[second_main + 1:second_main + 1 + len(
        battle_engine.COMBAT_STEPS)] == battle_engine.COMBAT_STEPS

def test_all_registered_effects_use_the_context_adapter():
    """Keep the nine-argument ABI behind one registry boundary."""
    assert _LEAFS
    assert all(hasattr(leaf, "__wrapped__") for leaf in _LEAFS.values())


def test_ability_resolution_entrypoint_accepts_context(monkeypatch):
    from abilities import resolve_ability_context

    context = EffectContext.from_rules_port(
        object(), _Session(), _DB(), object(), "player", "ai", {},
        "ability-guid", "")
    seen = {}

    def fake_resolve(handler, game, session, db, player_uid, ai_uid, bstate,
                     ability_guid, source_uid, owner_id, target_map,
                     variables=None, activation_data=None):
        seen.update({
            "handler": handler, "game": game, "ability_guid": ability_guid,
            "source_uid": source_uid, "owner_id": owner_id,
            "target_map": target_map,
        })
        return "resolved"

    monkeypatch.setattr("abilities.framework.resolution.resolve_ability",
                        fake_resolve)
    assert resolve_ability_context(
        context, source_uid=123, owner_id=5, target_map={0: 123}) == \
        "resolved"
    assert seen["ability_guid"] == "ability-guid"
    assert seen["source_uid"] == 123
    assert seen["owner_id"] == 5
    assert seen["target_map"] == {0: 123}


def test_context_draw_preserves_owner_and_handler_boundary():
    handler = _DrawHandler()
    handler.user_profile = {"id": 5}
    context = EffectContext.from_rules_port(
        object(), _Session(), _DB(), handler, "player", "ai",
        {"resolving_target_uid": 5}, "draw-effect", "")

    with mock.patch("rules_port.draw_effects.draw_cards",
                    return_value="draw 2 for owner 5") as draw:
        assert context.draw(2) == "draw 2 for owner 5"
    draw.assert_called_once_with(context, 2, owner=5)


def test_context_conversation_uses_typed_id_and_pauses_at_handler_boundary():
    handler = _ConversationHandler()
    context = EffectContext.from_rules_port(
        _Game(), _Session(), _DB(), handler, "player", "ai",
        {"resolving_ability": "ability-guid",
         "resolving_source_uid": 123,
         "resolving_effect_order": 4},
        "conversation-effect", "")
    with mock.patch.object(
            handler, "_queue_conversation_prompt", handler.queue, create=True), \
         mock.patch.object(
             context, "template_value",
             return_value="11111111-2222-3333-4444-555555555555"):
        assert context.conversation() == "queued"
    assert context.bstate["queued_conversation"] == \
        "11111111-2222-3333-4444-555555555555"
    assert context.bstate["resolution_paused"] is True


def test_tac_decoder_preserves_append_to_list_metadata():
    from abilities.framework.tac import decode_tac_tree, tac_function, tac_string

    # Representative authored TAC from Records.  Keeping the fixture local
    # makes this test independent of the developer's runtime database.
    param = (
        "AgD231pIDEFwcGVuZFRvTGlzdKx3EcQURXh0cmFDb21iYXRzVGhpc1R1cm4d"
        "ssWzDVRoaXNUdXJuc0RhdGEG67dBAQAAAAAAAAA="
    )
    tree = decode_tac_tree(param)
    assert tac_function(param) == "AppendToList"
    assert tac_string(param, "ListName") == "ExtraCombatsThisTurn"
    assert tac_string(param, "Where") == "ThisTurnsData"
    assert tree


def test_context_authored_events_use_typed_event_names():
    context = EffectContext.from_rules_port(
        object(), _Session(), _DB(), object(), "player", "ai",
        {"resolving_source_uid": 123, "resolving_owner_id": 5},
        "fire-effect", "")
    with mock.patch.object(
            context, "template_value",
            side_effect=lambda name, default=None: {
                "m_TriggerType": "Game.Shared.Mechanics.FateweavedEvent",
                "m_Name": "fireFateweavedEvent",
            }.get(name, default)), mock.patch.object(
                context, "_emit_trigger",
                return_value="triggered") as emit:
        assert context.fire_event() == "fired FateweavedEvent: triggered"
    assert emit.call_args.args[:3] == ("FateweavedEvent", 123, 5)


def test_native_transform_uses_typed_stored_target_copy():
    """UseStoredTarget copies the resolved target's template to the stored card."""
    context = EffectContext.from_rules_port(
        _Game(), _Session(), _DB(), object(), "player", "ai",
        {"resolving_ability": "copy-ability",
         "resolving_source_uid": 100,
         "stored_targets": {"copy-ability": [100]}},
        "copy-effect", effect_targets=(200,))
    typed = {
        "m_Portal": False,
        "m_CardTemplateId": "0" * 36,
        "m_SerializedTAC": {"data": "AgCct9UYAQAAAAAAAAA="},
    }
    with mock.patch.object(
            context, "template_value",
            side_effect=lambda name, default=None: typed.get(name, default)), \
            mock.patch("pvp_db.db_card_source_info",
                       return_value=("target-template", "Troop", "warzone", 5)), \
            mock.patch("rules_port.transform_effects.transform_instance",
                       return_value=100) as transform:
        result = context.transform_card()

    assert result == "transformed 1 stored card(s) -> target-t"
    transform.assert_called_once_with(
        context, 100, "target-template", keep_zone=True,
        copy_from_uid=200, event_gems=game_engine.EGemTypesNew.Unknown)


def test_native_same_collection_transform_emits_no_wire_card_moved():
    """Transforms restore their saved gems without adding a class-50 move."""
    from rules_port.transform_effects import revert_instance, transform_instance

    events = []

    class ProjectionGame:
        def push_card_transformed(self, *_args, **_kwargs):
            events.append("transformed")

        def push_card_reverted(self, *_args, **_kwargs):
            events.append("reverted")

        def push_card_updated(self, *_args, **_kwargs):
            events.append("updated")

        def push_card_moved(self, *_args, **_kwargs):
            events.append("moved")

    player_uid = game_engine.UID.make(244, 5)
    ai_uid = game_engine.UID.make(3, 1000)
    context = SimpleNamespace(
        game=ProjectionGame(), session=_Session(), db=_DB(),
        handler=SimpleNamespace(_card_full_data=lambda *_args: (
            "new-template", game_engine.ECardTypes.Troop, "Target", 1, 2, 2, 0)),
        player_uid=player_uid, ai_uid=ai_uid, bstate={})

    with mock.patch("pvp_db.db_card_owner_location_position",
                    return_value=(5, "warzone", 4)), \
            mock.patch("pvp_db.db_card_owner_zone_state",
                       return_value=(5, "warzone", 4, 0)), \
            mock.patch("pvp_db.db_card_sacrifice_info",
                       return_value=(5, "warzone", "Troop")), \
            mock.patch("pvp_db.db_copy_template_payload",
                       return_value=(game_engine.ECardTypes.Troop, "[]", 0)), \
            mock.patch("pvp_db.db_capture_original_gems",
                       side_effect=lambda *_args, **_kwargs:
                       events.append("captured original gems")), \
            mock.patch("pvp_db.db_card_original_gems", return_value=8), \
            mock.patch("pvp_db.db_card_original_and_template",
                       return_value=("original-template", "new-template")), \
            mock.patch("pvp_db.db_reset_card_modifiers"), \
            mock.patch("pvp_db.db_reset_card_uses"), \
            mock.patch("pvp_db.db_card_attribute_value", return_value=0), \
            mock.patch("rules_port.triggers.dispatch_trigger",
                       side_effect=lambda _context, event_type, *_args,
                       **_kwargs: events.append(event_type)) as trigger, \
            mock.patch("pvp_db.db_transform_card_instance",
                       side_effect=lambda *_args, **_kwargs:
                       events.append("mutated")) as transform_db:
        # Transforming a Warzone card to the same collection resets its
        # position; this used to add an unsupported standalone class-50 event.
        # C# also handles CardTransformsEvent before the mutation and queues
        # CardTransformedEvent after it.
        assert transform_instance(
            context, 0x102, "new-template") == 0x102
        assert revert_instance(context, 0x102) == "reverted 0x102"

    assert [call.args[1] for call in trigger.call_args_list] == [
        "CardTransformsEvent", "CardTransformedEvent"]
    transform_calls = transform_db.call_args_list
    assert len(transform_calls) == 2, transform_calls
    assert transform_calls[0].kwargs.get("gems") is None
    assert transform_calls[1].kwargs.get("gems") == 8
    assert events == ["CardTransformsEvent", "captured original gems",
                      "mutated", "transformed", "updated",
                      "CardTransformedEvent", "mutated", "reverted",
                      "updated"], events


def test_persisted_original_gems_survive_transform_and_revert():
    """Persist the first active gems and restore them on a later reversion."""
    import sqlite3
    import pvp_db

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE game_cards (session_id INTEGER, card_uid INTEGER, "
        "template_guid TEXT, original_template_guid TEXT, "
        "card_template_id TEXT, card_type TEXT, "
        "card_abilities TEXT, card_attributes INTEGER, location TEXT, "
        "position INTEGER, card_state INTEGER, temporary_attributes INTEGER "
        "DEFAULT 0, temporary_buffs TEXT DEFAULT '{}', gems INTEGER, "
        "original_gems INTEGER)"
    )
    conn.execute(
        "INSERT INTO game_cards (session_id, card_uid, template_guid, "
        "original_template_guid, card_template_id, card_type, card_abilities, card_attributes, "
        "location, position, card_state, gems) "
        "VALUES (9, 258, 'original-template', 'original-template', "
        "'original-template', 'Troop', '[]', 0, 'warzone', 4, 0, 8)"
    )

    assert pvp_db.db_capture_original_gems(9, 258, conn=conn) == 1
    pvp_db.db_transform_card_instance(
        9, 258, "transformed-template", "Troop", "[]", 0,
        "warzone", 4, 0, conn=conn, gems=32)
    assert pvp_db.db_card_original_gems(9, 258, conn=conn) == 8
    assert pvp_db.db_card_gem_type(9, 258, conn=conn) == 32

    # Later transforms must keep C#'s first-original value, including when
    # that value is the valid empty-socket enum bit.
    assert pvp_db.db_capture_original_gems(9, 258, conn=conn) == 0
    pvp_db.db_transform_card_instance(
        9, 258, "original-template", "Troop", "[]", 0,
        "warzone", 4, 0, conn=conn,
        gems=pvp_db.db_card_original_gems(9, 258, conn=conn))
    assert pvp_db.db_card_gem_type(9, 258, conn=conn) == 8

    conn.execute("UPDATE game_cards SET original_gems=NULL WHERE card_uid=258")
    conn.execute("UPDATE game_cards SET gems=? WHERE card_uid=258", (1 << 62,))
    assert pvp_db.db_capture_original_gems(9, 258, conn=conn) == 1
    assert pvp_db.db_card_original_gems(9, 258, conn=conn) == 1 << 62
    conn.execute("UPDATE game_cards SET gems=32 WHERE card_uid=258")
    assert pvp_db.db_capture_original_gems(9, 258, conn=conn) == 0
    assert pvp_db.db_card_original_gems(9, 258, conn=conn) == 1 << 62

    conn.execute(
        "INSERT INTO game_cards (session_id, card_uid, template_guid, "
        "original_template_guid, card_template_id, card_type, card_abilities, "
        "card_attributes, location, position, card_state, gems) "
        "VALUES (9, 259, 'transformed-template', 'original-template', "
        "'transformed-template', 'Troop', '[]', 0, 'warzone', 4, 0, 32)"
    )
    assert pvp_db.db_capture_original_gems(9, 259, conn=conn) == 0
    assert pvp_db.db_card_original_gems(9, 259, conn=conn) is None
    pvp_db.db_transform_card_instance(
        9, 259, "original-template", "Troop", "[]", 0,
        "warzone", 4, 0, conn=conn,
        gems=pvp_db.db_card_original_gems(9, 259, conn=conn))
    assert pvp_db.db_card_gem_type(9, 259, conn=conn) == 32
    conn.close()


def test_native_transform_event_uses_pre_transform_viewer_visibility():
    """Class 44 identity is limited to viewers of the old card/collection."""
    from rules_port.transform_effects import transform_instance

    owner = game_engine.UID.make(244, 1001)
    controller = game_engine.UID.make(244, 1002)
    template = "cccccccc-cccc-cccc-cccc-cccccccccccc"

    class Packet:
        def __init__(self):
            self.events = []

        def add_event(self, event):
            self.events.append(event)

    def transform_packets(visibility=None, *, zone="hand"):
        game = game_engine.Game(1, owner, controller)
        game._visibility_by_uid[int(owner.uid64)] = dict(visibility or {})
        context = SimpleNamespace(
            game=game, session=_Session(), db=_DB(),
            handler=SimpleNamespace(
                user_profile={"id": 1001},
                _card_full_data=lambda *_args: (
                    template, game_engine.ECardTypes.Troop, "Target", 1, 2, 2, 0)),
            player_uid=owner, ai_uid=controller,
            bstate={"pvp": True, "pids": [1001, 1002]})
        with mock.patch("pvp_db.db_card_owner_location_position",
                        return_value=(1002, zone, 3)), \
                mock.patch("pvp_db.db_card_owner_zone_state",
                           return_value=(1002, zone, 0)), \
                mock.patch("pvp_db.db_card_sacrifice_info",
                           return_value=(1002, zone, "Troop")), \
                mock.patch("pvp_db.db_deck_top_card_details",
                           return_value=(1, 0x6604, "instance", template)), \
                mock.patch("rules_port.static_rules.player_int_attributes",
                           side_effect=lambda _db, _sid, _state, player:
                           {"CanSeeOpponentsTopOfDeck": 1}
                           if int(player) == 1001 else {}), \
                mock.patch("pvp_db.db_copy_template_payload",
                           return_value=("Troop", "[]", 0)), \
                mock.patch("pvp_db.db_transform_card_instance"), \
                mock.patch("pvp_db.db_reset_card_uses"), \
                mock.patch("pvp_db.db_card_attribute_value", return_value=0), \
                mock.patch("rules_port.triggers.dispatch_trigger"):
            assert transform_instance(context, 0x6604, template) == 0x6604
        batch = list(game.events)
        with mock.patch("domain.game.NetworkPacketSessionEventArgs", Packet):
            owner_packet = game.make_network_packet(owner)
            game.events = batch
            opponent_packet = game.make_network_packet(controller)
        return owner_packet, opponent_packet

    owner_packet, controller_packet = transform_packets()
    assert not any(isinstance(event, game_engine.CardTransformedSessionEventArgs)
                   for event in owner_packet.events)
    assert any(isinstance(event, game_engine.CardTransformedSessionEventArgs)
               for event in controller_packet.events)
    owner_updates = [event for event in owner_packet.events
                    if isinstance(event, game_engine.CardUpdatedSessionEventArgs)]
    assert owner_updates and owner_updates[-1].nulling is True
    assert owner_updates[-1].card_id.guid.int == 0

    _owner_packet, authorized_opponent_packet = transform_packets(
        {"CanSeeOpponentsHand": 1})
    assert any(isinstance(event, game_engine.CardTransformedSessionEventArgs)
               for event in authorized_opponent_packet.events)

    deck_view_packet, deck_controller_packet = transform_packets(zone="deck")
    assert any(isinstance(event, game_engine.CardTransformedSessionEventArgs)
               for event in deck_view_packet.events)
    assert not any(isinstance(event, game_engine.CardTransformedSessionEventArgs)
                   for event in deck_controller_packet.events)


def test_card_transform_reset_and_category_gates_match_csharp():
    """Check C# use-reset boundaries and type-transition revert gates."""
    from pvp_db import db_reset_card_uses
    import json

    initial = {
        "per-game-ability": 2,
        "__turn_uses__": {"4:per-turn-ability": 1},
        "__cooldown_counts__": {"cooldown-ability": 2},
    }

    class Cursor:
        rowcount = 1

        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

    class UsageDB:
        def __init__(self):
            self.uses = dict(initial)

        def execute(self, sql, params):
            if sql.startswith("SELECT card_uses"):
                return Cursor((json.dumps(self.uses),))
            assert sql.startswith("UPDATE game_cards SET card_uses=")
            self.uses = json.loads(params[0])
            return Cursor()

    db = UsageDB()
    assert db_reset_card_uses(9, 10, conn=db) == 1
    assert db.uses == {
        "__turn_uses__": initial["__turn_uses__"],
        "__cooldown_counts__": initial["__cooldown_counts__"],
    }

    db.uses = dict(initial)
    assert db_reset_card_uses(
        9, 10, preserve_turn_and_cooldowns=False, conn=db) == 1
    assert db.uses == {}

    from rules_port.transform_effects import _requires_transform_revert
    assert _requires_transform_revert(
        game_engine.ECardTypes.Troop,
        game_engine.ECardTypes.BasicAction)
    assert _requires_transform_revert(
        game_engine.ECardTypes.Resource,
        game_engine.ECardTypes.Constant)
    assert not _requires_transform_revert(
        game_engine.ECardTypes.BasicAction,
        game_engine.ECardTypes.Resource)
    assert not _requires_transform_revert(
        game_engine.ECardTypes.Constant,
        game_engine.ECardTypes.Artifact)


def test_copy_transform_copies_supported_csharp_mutable_card_fields():
    """TransformIntoCopy carries the persisted subset of Card.CopyFrom."""
    import json
    import sqlite3
    from pvp_db import db_copy_transform_instance_state

    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE game_cards (session_id INTEGER, card_uid INTEGER, "
        "gems INTEGER, card_damage INTEGER, card_uses TEXT, "
        "permanent_buffs TEXT, temporary_buffs TEXT)")
    destination_permanent = {
        "counters": {"Charge": 2, "DestinationOnly": 4},
        "counter_guids": {"Charge": "charge-guid",
                           "DestinationOnly": "destination-guid"},
        "card_integer_variables": {"discarded": 9},
        "escalation_count": 3,
        "damage_shields": [{"amount": 1, "owner": "destination"}],
    }
    destination_temporary = {
        "damage_shields": [{"amount": 2, "owner": "destination-temp"}],
        "stat_modifiers": [{"amount": 7}],
    }
    source_permanent = {
        "counters": {"Charge": 5, "SourceOnly": 1},
        "counter_guids": {"Charge": "charge-guid",
                           "SourceOnly": "source-guid"},
        "card_integer_variables": {"copied": 12},
        "escalation_count": 8,
        "damage_shields": [{"amount": 3, "owner": "source"}],
    }
    source_temporary = {
        "damage_shields": [{"amount": 4, "owner": "source-temp"}],
        "stat_modifiers": [{"amount": 11}],
    }
    source_uses = {
        "ability-guid": 2,
        "__turn_uses__": {"turn-count": 3},
        "__cooldown_counts__": {"cooldown": 1},
    }
    db.executemany(
        "INSERT INTO game_cards VALUES (9, ?, ?, ?, ?, ?, ?)",
        [
            (100, 1, 2, '{"destination-use":1}',
             json.dumps(destination_permanent),
             json.dumps(destination_temporary)),
            (200, 4, 5, json.dumps(source_uses),
             json.dumps(source_permanent), json.dumps(source_temporary)),
        ])

    assert db_copy_transform_instance_state(9, 100, 200, conn=db) == 1
    row = db.execute(
        "SELECT gems, card_damage, card_uses, permanent_buffs, "
        "temporary_buffs FROM game_cards WHERE session_id=9 AND card_uid=100"
    ).fetchone()
    assert row[0:2] == (4, 5)
    assert json.loads(row[2]) == source_uses
    permanent = json.loads(row[3])
    temporary = json.loads(row[4])
    assert permanent["counters"] == {
        "Charge": 7, "DestinationOnly": 4, "SourceOnly": 1}
    assert permanent["counter_guids"]["SourceOnly"] == "source-guid"
    assert permanent["card_integer_variables"] == {"copied": 12}
    assert permanent["escalation_count"] == 8
    assert [shield["owner"] for shield in permanent["damage_shields"]] == [
        "destination", "source"]
    assert [shield["owner"] for shield in temporary["damage_shields"]] == [
        "destination-temp", "source-temp"]
    assert temporary["stat_modifiers"] == [{"amount": 7}]

    replica_destination = dict(permanent)
    replica_destination["card_integer_variables"] = {"replica-only": 6}
    replica_destination["escalation_count"] = 2
    db.execute(
        "UPDATE game_cards SET card_damage=17, card_uses=?, permanent_buffs=? "
        "WHERE session_id=9 AND card_uid=100",
        ('{"replica-destination":1}', json.dumps(replica_destination)))
    assert db_copy_transform_instance_state(
        9, 100, 200, is_replica=True, conn=db) == 1
    replica_row = db.execute(
        "SELECT gems, card_damage, card_uses, permanent_buffs "
        "FROM game_cards WHERE session_id=9 AND card_uid=100"
    ).fetchone()
    assert replica_row[:3] == (4, 17, '{"replica-destination":1}')
    replica_permanent = json.loads(replica_row[3])
    assert replica_permanent["card_integer_variables"] == {"copied": 12}
    assert replica_permanent["escalation_count"] == 8
    assert replica_permanent["counters"] == permanent["counters"]


def test_native_transform_to_target_preserves_source_collection():
    """TransformIntoCopy changes the source template in its current zone."""
    context = EffectContext.from_rules_port(
        _Game(), _Session(), _DB(), object(), "player", "ai",
        {"resolving_source_uid": 100, "resolving_target_uid": 200},
        "transform-to-target")
    with mock.patch("pvp_db.db_card_zone_details",
                    return_value=("target-template",)), \
            mock.patch("rules_port.transform_effects.transform_instance",
                       return_value=100) as transform:
        result = context.transform_card_to_target()

    assert result == "transformed 0x64 to target copy", result
    transform.assert_called_once_with(
        context, 100, "target-template", keep_zone=True,
        copy_from_uid=200, is_replica=False)


# The Verdict effect builds its typed choice tokens and activates the
# authored choose/play ability; its focused fixture lives in
# tests/tests_rules_port_kernel.py (test_verdict_creates_pool_tokens...).
# The VerdictEvent itself is published by the sibling FireEvent effect.


def test_context_draw_effect_uses_typed_or_fixture_count():
    handler = _DrawHandler()
    handler.user_profile = {"id": 5}
    context = EffectContext.from_rules_port(
        object(), _Session(), _DB(), handler, "player", "ai",
        {"resolving_target_uid": 5}, "missing-draw-effect",
        '{"count": 2}')

    with mock.patch("rules_port.draw_effects.draw_cards",
                    return_value="draw 2 for owner 5") as draw:
        assert context.draw_effect() == "draw 2 for owner 5"
    draw.assert_called_once_with(context, 2, owner=5)


def test_native_draw_routes_empty_deck_to_explicit_result_projection():
    from types import SimpleNamespace
    from rules_port.draw_effects import draw_cards

    session = SimpleNamespace(session_id=9)
    game = SimpleNamespace()
    seen = []
    context = SimpleNamespace(
        db=_DB(), session=session, game=game, handler=SimpleNamespace(
            _rules_port_deck_out=lambda *args: seen.append(args)),
        player_uid="player", ai_uid="ai", bstate={},
        target_owner=lambda default=None: 5,
        _emit_trigger=lambda *args, **kwargs: False,
    )
    with mock.patch("pvp_db.db_deck_top_card_details", return_value=None):
        assert draw_cards(context, 1, owner=5) == "draw 0 for owner 5"
    assert len(seen) == 1
    assert seen[0][0:2] == (game, session)
    assert seen[0][2] == 5


def test_card_zone_projection_uses_client_derived_moves():
    from rules_port.choice_effects import (
        _clear_choice_zone, _create_choice_cards)
    from rules_port.deck_effects import move_deck_card_to_hand
    from rules_port.draw_effects import draw_cards
    from rules_port.host_mutations import project_mulligan_cards

    class RecordingGame:
        def __init__(self):
            self.events = []

        def push_card_updated(self, *args, **kwargs):
            self.events.append(("updated", args[2], kwargs))

        def push_card_moved(self, *args, **kwargs):
            self.events.append(("moved", args[2], kwargs))

        def push_card_drawn(self, *args, **kwargs):
            self.events.append(("drawn", args[1], kwargs))

    game = RecordingGame()
    player_uid = game_engine.UID.make(244, 5)
    handler = SimpleNamespace(
        _player_champ_scid=None, _ai_champ_scid=None,
        _card_full_data=lambda *_args: (
            "template-guid", game_engine.ECardTypes.Troop, "Troop", 2, 1, 3,
            []))
    context = SimpleNamespace(
        db=_DB(), session=SimpleNamespace(session_id=9), game=game,
        handler=handler, player_uid=player_uid,
        ai_uid=game_engine.UID.make(244, 6), bstate={},
        target_owner=lambda default=None: 5,
        _emit_trigger=lambda *_args, **_kwargs: False)

    with mock.patch("pvp_db.db_deck_top_card_details",
                    return_value=(12, 77, "Troop", "template-guid")), \
            mock.patch("pvp_db.db_draw_card_to_hand"):
        assert draw_cards(context, 1, owner=5) == "draw 1 for owner 5"
    assert [kind for kind, _collection, _fields in game.events] == [
        "updated", "drawn"]
    assert game.events[0][1] == game_engine.ECardCollections.Hand

    game.events.clear()
    with mock.patch("pvp_db.db_copy_template_payload",
                    return_value=("Troop", "[]", "{}")), \
            mock.patch("pvp_db.db_next_game_card_row_id", return_value=1), \
            mock.patch("pvp_db.db_insert_generated_card"), \
            mock.patch("rules_port.runtime_helpers.next_game_card_uid",
                       return_value=80):
        assert _create_choice_cards(context, 5, ["template-guid"]) == [80]
    assert [kind for kind, _collection, _fields in game.events] == [
        "updated"]
    assert game.events[0][1] == game_engine.ECardCollections.Choosing

    game.events.clear()
    with mock.patch("pvp_db.db_choice_card_rows", return_value=[
            (79, 5, "template-guid", "Troop")]), \
            mock.patch("pvp_db.db_move_choice_to_played_resources") as move:
        _clear_choice_zone(context)
    move.assert_called_once_with(9, 79, conn=context.db)
    assert [kind for kind, _collection, _fields in game.events] == [
        "updated"]
    assert game.events[0][1] == game_engine.ECardCollections.PlayedResources

    game.events.clear()
    project_mulligan_cards(
        handler, game, player_uid, [(0, 76)],
        [(78, "Troop", "template-guid")], new_player_uid=player_uid,
        draw_new=True)
    assert [kind for kind, _collection, _fields in game.events] == [
        "updated", "updated", "drawn"]
    assert game.events[0][1] == game_engine.ECardCollections.Deck
    assert game.events[1][1] == game_engine.ECardCollections.Hand

    game.events.clear()
    with mock.patch("pvp_db.db_card_zone_details",
                    return_value=("template-guid", "Troop", 5, "deck")), \
            mock.patch("pvp_db.db_move_card_to_location"):
        assert move_deck_card_to_hand(context, 77, 5) == \
            "searched deck card 0x4d to hand"
    assert [kind for kind, _collection, _fields in game.events] == [
        "updated", "drawn"]
    assert game.events[0][1] == game_engine.ECardCollections.Hand


def test_context_randomize_variable_uses_typed_bounds():
    context = EffectContext.from_rules_port(
        object(), _Session(), _DB(), object(), "player", "ai", {},
        "random-effect", "")
    with mock.patch.object(
            context, "template_value",
            side_effect=lambda name, default=None: {
                "m_VariableName": "RandomNumber",
            }.get(name, default)), mock.patch.object(
                context, "value",
                side_effect=lambda name, default=0: {
                    "m_MinValue": 2,
                    "m_MaxValue": 5,
                }.get(name, default)), mock.patch(
                    "random.randint", return_value=4) as randint:
        assert context.randomize_variable() == "randomized RandomNumber=4"
    assert context.bstate["ability_variables"]["RandomNumber"] == 4
    randint.assert_called_once_with(2, 5)


def test_context_stat_mod_uses_shared_operation_boundary():
    context = EffectContext.from_rules_port(
        object(), _Session(), _DB(), object(), "player", "ai", {},
        "stat-effect", "")
    with mock.patch(
            "rules_port.stat_effects.apply_stat_mod", return_value=4) as apply:
        assert context.stat_mod(123, 2, 3, this_turn=True) == 4
    assert apply.call_args.args[1:4] == (123, 2, 3)
    assert apply.call_args.kwargs == {"this_turn": True}


def test_context_damage_modifier_resolves_typed_input_and_target():
    context = EffectContext.from_rules_port(
        _Game(), _Session(), _DB(), object(), "player", "ai",
        {
            "resolving_ability":
            "af816e98-51dd-9029-2e0a-5f3be27f1689",
            "resolving_target_uid": 123,
            "resolving_owner_id": 1,
        },
        "796ff24c-d468-68c1-8636-82d26a149c8b", "")
    with mock.patch.object(context, "damage", return_value="damaged") as damage:
        assert context.damage_modifier(
            {"text": "Deal 2 damage", "amount": 0},
            {"input_variable": "2"}) == "damaged"
    damage.assert_called_once_with(123, 2)


def test_context_stat_modifier_resolves_typed_input_and_duration():
    context = EffectContext.from_rules_port(
        _Game(), _Session(), _DB(), object(), "player", "ai",
        {
            "resolving_ability":
            "af816e98-51dd-9029-2e0a-5f3be27f1689",
            "resolving_target_uid": 123,
            "resolving_owner_id": 1,
        },
        "796ff24c-d468-68c1-8636-82d26a149c8b", "")
    with mock.patch.object(context, "stat_mod", return_value=4) as stat_mod:
        assert context.stat_modifier(
            {"property": "attack", "amount": 0,
             "input_variable": "2", "duration": "EndOfTurn"},
            {"property": "attack", "input_variable": "2"}) == \
            "mod 0x7b +2/+0"
    stat_mod.assert_called_once_with(123, 2, 0, this_turn=True)


def test_context_counter_hides_card_persistence_and_projection():
    context = EffectContext.from_rules_port(
        _Game(), _Session(), _DB(), object(), "player", "ai", {},
        "counter-effect", "")
    with mock.patch(
            "rules_port.counter_effects.change_counter",
            return_value=(3, 1)) as change:
        assert context.counter(123, "roar", amount=2,
                               operation="remove") == (3, 1)
    change.assert_called_once()
    assert change.call_args.args[1:3] == (123, "roar")
    assert change.call_args.args[5] == "remove"


def test_context_counter_uses_champion_state_and_event_projection():
    context = EffectContext.from_rules_port(
        _Game(), _Session(), _DB(), object(), "player", "ai", {},
        "counter-effect", "")
    with mock.patch(
            "rules_port.counter_effects.change_counter",
            return_value=(1, 4)) as change:
        assert context.counter(123, "roar", "counter-guid", 3) == (1, 4)
    change.assert_called_once()
    assert change.call_args.args[1:4] == (123, "roar", "counter-guid")


def test_rules_port_counter_projection_reads_persisted_buffs():
    """``_project`` must read ``permanent_buffs`` through the typed accessor.

    ``db_card_source_info`` returns four columns; indexing ``row[4]`` raised
    IndexError during the Tunneling turn-start advance and closed the game
    connection ("stuck at end-phase").
    """
    from rules_port.counter_effects import _project

    class _Game:
        def __init__(self):
            self.updated = []
            self.counters = []

        def push_card_updated(self, *args, **kwargs):
            self.updated.append((args, kwargs))

        def push_card_counters_changed(self, *args, **kwargs):
            self.counters.append((args, kwargs))

    class _Handler:
        def _card_full_data(self, game, scid, template):
            return (template, "Troop", "Spy", 1, 1, 1, 0)

    game = _Game()
    context = SimpleNamespace(
        game=game, session=_Session(), db=_DB(), handler=_Handler(),
        player_uid="player", ai_uid="ai", bstate={})

    with mock.patch(
            "pvp_db.db_card_source_info",
            return_value=("tpl-guid", "Troop", "underground", 0)), \
            mock.patch(
                "pvp_db.db_card_mutation_field",
                return_value='{"counters": {"tunneling": 2}}'), \
            mock.patch(
                "pvp_db.db_counter_template_id",
                return_value="def75520-0b8b-447f-8705-b34e71043890"), \
            mock.patch("pvp_db.db_card_owner_id", return_value=0):
        _project(context, 1281, 0,
                 "def75520-0b8b-447f-8705-b34e71043890", 1, 2)

    assert game.counters, "counter projection must emit its change event"
    assert game.updated, "counter projection must emit a CardUpdated"


def test_context_remove_from_combat_preserves_state_event_boundary():
    context = EffectContext.from_rules_port(
        _Game(), _Session(), _DB(), object(), "player", "ai",
        {"resolving_target_uid": 123}, "combat-effect", "")
    with mock.patch("rules_port.card_projection.push_card_state") as push:
        assert context.remove_from_combat() == "removed 0x7b from combat"
    push.assert_called_once()


def test_context_card_state_owns_projection_and_tap_trigger():
    context = EffectContext.from_rules_port(
        _Game(), _Session(), _DB(), object(), "player", "ai", {},
        "tap-effect", "")
    with mock.patch("rules_port.card_projection.push_card_state") as push, \
            mock.patch.object(
                context, "_emit_trigger", return_value=False) as emit:
        assert context.update_card_state(
            123, add=1, trigger="CardTappedEvent") == 5
    push.assert_called_once()
    assert emit.call_args.args[:2] == ("CardTappedEvent", 123)


def test_context_lose_thresholds_preserves_state_and_event_operation():
    game = _Game()
    bstate = {"resolving_owner_id": 5, "player_threshold": {4: 2}}
    context = EffectContext.from_rules_port(
        game, _Session(), _DB(), object(), "player", "ai", bstate,
        "threshold-effect", "")

    assert context.lose_thresholds(["Blood"]) == "lost 2 threshold(s)"
    assert bstate["player_threshold"][4] == 0
    assert len(game.events) == 1
    assert game.events[0].operation == 2
    assert game.events[0].delta == 2


def test_context_typed_hero_health_and_spell_points_share_owner_projection():
    import game_engine

    game = _Game()
    bstate = {
        "resolving_owner_id": 5,
        "player_health": 20,
        "player_spell_points": 1,
    }
    context = EffectContext.from_rules_port(
        game, _Session(), _DB(), object(), "player", "ai", bstate,
        "typed-modifier", "")

    assert context.set_hero_health(123, 25) == "set health 20->25"
    assert context.spell_points(123, 2) == "spell points 1->3"
    assert bstate["player_health"] == 25
    assert bstate["player_spell_points"] == 3
    assert isinstance(game.events[0],
                      game_engine.ChampionHealthChangedSessionEventArgs)
    assert isinstance(game.events[1],
                      game_engine.ChampionSpellPointsChangedSessionEventArgs)


def test_context_typed_card_threshold_and_subtype_preserve_metadata_values():
    context = EffectContext.from_rules_port(
        _Game(), _Session(), _DB(), object(), "player", "ai", {},
        "typed-modifier", "")
    with mock.patch.object(context, "_card_thresholds", return_value=[4, 4]), \
            mock.patch.object(context, "_card_buffs", return_value={}), \
            mock.patch.object(context, "_save_card_buffs") as save, \
            mock.patch.object(context, "_push_modifier_card") as push:
        assert context.card_threshold(
            123, {"shard": "Ruby", "setthresholds": False}) == \
            "thresholds 0x7b -> [8, 8]"
        save.assert_called_once()
        push.assert_called_once_with(123, thresholds=[8, 8])

    with mock.patch.object(context, "_card_buffs",
                           return_value={"subtype": "Orc"}), \
            mock.patch.object(context, "_save_card_buffs") as save, \
            mock.patch.object(context, "_push_modifier_card") as push:
        assert context.subtype_modifier(
            123, {"subtype": "Robot", "operation": "Add"}) == \
            "subtype 0x7b -> Orc Robot"
        assert save.call_args.args[1]["subtype"] == "Orc Robot"
        push.assert_called_once_with(123, sub_type="Orc Robot")


def test_context_modifier_keeps_deck_cards_nulled():
    """Prophecy's persisted IntAttr update must not reveal its deck target."""
    class Game:
        def __init__(self):
            self.updated = []

        def push_card_updated(self, *args, **kwargs):
            self.updated.append((args, kwargs))

    class Handler:
        @staticmethod
        def _card_full_data(_game, _scid, template):
            return (template, "Troop", "Hidden troop", 2, 2, 2, 0)

    game = Game()
    context = EffectContext.from_rules_port(
        game, _Session(), _DB(), Handler(), "player", "ai", {}, "effect")
    with mock.patch("pvp_db.db_card_zone_details",
                    return_value=("template-guid", 0, 0, "deck")):
        context._push_modifier_card(123, int_attrs={"Prophesied": -2})

    assert game.updated[0][1]["nulling"] is True
    assert game.updated[0][1]["int_attrs"] == {"Prophesied": -2}


def test_context_typed_damage_shield_uses_client_flags():
    context = EffectContext.from_rules_port(
        _Game(), _Session(), _DB(), object(), "player", "ai", {},
        "typed-modifier", "")
    with mock.patch.object(context, "_card_buffs", return_value={}) as buffs, \
            mock.patch.object(context, "_save_card_buffs") as save, \
            mock.patch.object(context, "_push_modifier_card") as push:
        assert context.damage_shield(
            123, 3, {"onlycombatdamage": True, "oneshot": True,
                     "lastsindefinitely": False}) == \
            "damage shield 0x7b +3"
        entry = save.call_args.args[1]["damage_shields"][0]
        assert entry == {
            "amount": 3, "only_combat": True, "one_shot": True,
            "lasts_indefinitely": False, "rules_port_order": 0,
        }
        assert buffs.call_args_list == [
            mock.call(123, "permanent_buffs"),
            mock.call(123, "temporary_buffs"),
        ]
        push.assert_called_once_with(123, damage_shield=True)


def test_builder_reuses_metadata_cost_target_filter_and_ordering():
    first = EffectSpec(
        guid="effect-2", concrete_type="DrawNCardsAbilityEffectTemplate",
        operation="DrawNCards", name="draw", target_index=0,
        effect_instance_id=2, effect_group_id=2, duration="Instant",
        condition_guid="", optional=False, recalculate_targets="UseDefault",
        secondary_target_index=-1, output_variables={})
    second = EffectSpec(
        guid="effect-1", concrete_type="CardModifierAbilityEffectTemplate",
        operation="CardModifier", name="modify", target_index=0,
        effect_instance_id=1, effect_group_id=1, duration="Instant",
        condition_guid="", optional=False, recalculate_targets="UseDefault",
        secondary_target_index=-1, output_variables={})
    graph = AbilityGraph(
        guid="ability-guid", name="test", game_text="", activation_game_text="",
        casting_behavior="QuickAction", manual=True, optional=False,
        trigger_event_type="", trigger_collection_flags="", trigger_condition=None,
        ability_condition=None, ability_free_condition=None, ignores_chain=False,
        recalculate_auto_targets=False, uses_previous_state=False, ability_index=-1,
        options=(), additional_cost_targets=(),
        costs=AbilityCost(activation=2),
        targets=(TargetSpec(
            guid="target-guid", name="target", is_auto=False, is_random=False,
            player_filter="Opposing", collection_flags="Warzone", minimum=1,
            maximum=1, optional=False, explicit=True,
            card_filter={"m_CardType": "Troop"}),),
        effects=(first, second), variables=(), source=None)
    instance = AbilityInstance.from_graph(graph)
    plan = PlayPlan(
        card=object(),
        cost=CardPlayCost(resource=3, variable=False, variable_minimum=0,
                          life=0, threshold=None),
        abilities=(instance,))
    builder = AbilityBuilder.from_plan(plan, "ability-guid")

    assert builder.costs.activation == 2
    assert builder.card_costs.resource == 3
    assert builder.card_cost_instances == ()
    assert builder.filter_target(0) == {"m_CardType": "Troop"}
    assert builder.filter_target("primary") == {"m_CardType": "Troop"}
    assert [effect["effect_instance_id"] for effect in builder.effects] == [1, 2]
    assert builder.effect(0).type_name == "CardModifierAbilityEffectTemplate"
    assert builder.effect(0).condition_guid == ""
    assert [group for group, _ in builder.effect_groups] == [1, 2]
    assert builder.conditions == {"ability": None, "trigger": None, "free": None}
    assert builder.validate().count("missing target input at index 0") == 1
    bound = builder.bind(ActivationData.from_values(target_map={0: [123]}))
    assert bound.validate() == ()
    context = EffectContext.from_rules_port(
        object(), _Session(), _DB(), object(), "player", "ai",
        {}, "effect", "", ability=builder)
    assert context.ability is builder


def test_builder_exposes_typed_values_conditions_and_continuations():
    first = EffectSpec(
        guid="effect-1", concrete_type="DrawNCardsAbilityEffectTemplate",
        operation="DrawNCards", name="draw", target_index=0,
        effect_instance_id=1, effect_group_id=1, duration="Instant",
        condition_guid="condition-1", optional=False,
        recalculate_targets="UseDefault", secondary_target_index=-1,
        output_variables={})
    second = EffectSpec(
        guid="effect-2", concrete_type="ActivateAbilityEffectTemplate",
        operation="ActivateAbility", name="continue", target_index=-1,
        effect_instance_id=2, effect_group_id=2, duration="Instant",
        condition_guid="", optional=False, recalculate_targets="UseDefault",
        secondary_target_index=-1, output_variables={})
    graph = AbilityGraph(
        guid="ability-guid", name="test", game_text="", activation_game_text="",
        casting_behavior="QuickAction", manual=True, optional=False,
        trigger_event_type="", trigger_collection_flags="", trigger_condition=None,
        ability_condition=None, ability_free_condition=None, ignores_chain=False,
        recalculate_auto_targets=False, uses_previous_state=False, ability_index=-1,
        options=(), additional_cost_targets=(), costs=AbilityCost(activation=0),
        targets=(), effects=(first, second), variables=(), source=None)
    builder = AbilityBuilder.from_graph(graph)

    assert builder.effect_conditions == {0: "condition-1"}
    assert [ref.type_name for ref in builder.continuations] == [
        "ActivateAbilityEffectTemplate"]
    with mock.patch(
            "rules_port.bom_fields.effect_field", return_value=6) as field:
        assert builder.value(_DB(), {"session_id": 9}, "m_InputValue",
                             effect=builder.effect(0)) == 6
    field.assert_called_once()


def test_builder_centralizes_card_cost_candidates():
    first = EffectSpec(
        guid="effect-1", concrete_type="DrawNCardsAbilityEffectTemplate",
        operation="DrawNCards", name="draw", target_index=-1,
        effect_instance_id=1, effect_group_id=1, duration="Instant",
        condition_guid="", optional=False, recalculate_targets="UseDefault",
        secondary_target_index=-1, output_variables={})
    graph = AbilityGraph(
        guid="ability-guid", name="test", game_text="", activation_game_text="",
        casting_behavior="QuickAction", manual=True, optional=False,
        trigger_event_type="", trigger_collection_flags="", trigger_condition=None,
        ability_condition=None, ability_free_condition=None, ignores_chain=False,
        recalculate_auto_targets=False, uses_previous_state=False, ability_index=-1,
        options=(), additional_cost_targets=(),
        costs=AbilityCost(activation=0), targets=(),
        effects=(first,), variables=(), source=None)
    plan = PlayPlan(
        card=object(),
        cost=CardPlayCost(
            resource=0, variable=False, variable_minimum=0, life=0,
            threshold=None,
            additional_cost_targets=(("sacrifice", "cost-guid"),)),
        abilities=(AbilityInstance.from_graph(graph),))
    builder = AbilityBuilder.from_play_plan(plan)

    with mock.patch(
            "abilities.framework.targeting.legal_targets_for",
            return_value=[44, 45]) as legal:
        result = builder.card_cost_candidates(
            _DB(), 9, 1, source_uid=77, battle_state={})

    assert result[0][0]["target_guid"] == "cost-guid"
    assert result[0][1] == (44, 45)
    assert legal.call_args.kwargs == {
        "both_players": False, "champions": None, "battle_state": {}}


def test_builder_target_and_cost_candidates_delegate_to_shared_legality():
    first = EffectSpec(
        guid="effect", concrete_type="DrawNCardsAbilityEffectTemplate",
        operation="DrawNCards", name="draw", target_index=0,
        effect_instance_id=1, effect_group_id=1, duration="Instant",
        condition_guid="", optional=False, recalculate_targets="UseDefault",
        secondary_target_index=-1, output_variables={})
    graph = AbilityGraph(
        guid="ability-guid", name="test", game_text="", activation_game_text="",
        casting_behavior="QuickAction", manual=True, optional=False,
        trigger_event_type="", trigger_collection_flags="", trigger_condition=None,
        ability_condition=None, ability_free_condition=None, ignores_chain=False,
        recalculate_auto_targets=False, uses_previous_state=False, ability_index=-1,
        options=(), additional_cost_targets=(),
        costs=AbilityCost(activation=0),
        targets=(TargetSpec(
            guid="target-guid", name="target", is_auto=False, is_random=False,
            player_filter="Opposing", collection_flags="Warzone", minimum=1,
            maximum=1, optional=False, explicit=True,
            card_filter={"m_CardType": "Troop"}),),
        effects=(first,), variables=(), source=None)
    builder = AbilityBuilder.from_graph(graph)
    with mock.patch(
            "rules_port.targeting.legal_targets_for",
            return_value=[123]) as legal:
        assert builder.target_candidates(
            _DB(), 9, 1, builder.target(0), 55) == [123]
    legal.assert_called_once()


def test_battle2cards_uses_authored_secondary_effect_target():
    """The referenced effect target wins over older stored targets."""
    from rules_port.effects import dispatch

    ability_guid = "shared-battle-ability"
    first_source, first_target = 0x801, 0x4701
    second_source, second_target = 0x301, 0x4401
    attack = {
        first_source: 3, first_target: 2,
        second_source: 6, second_target: 1,
    }
    damage = []
    battle_effect = SimpleNamespace(
        effect_instance_id=1, target_index=1, secondary_target_index=0)
    effects = (
        # The secondary value indexes an effect instance; that effect's
        # target_index may differ from both the secondary value and the
        # Battle2Cards effect's own target index.
        SimpleNamespace(effect_instance_id=0, target_index=2),
        battle_effect,
    )

    def resolve(source, target, old_stored):
        target_map = {1: (target,), 2: (source,)}
        bstate = {
            "resolving_ability": ability_guid,
            "resolving_source_uid": source,
            "resolving_target_uid": target,
            "ability_target_map": target_map,
            "stored_targets": {ability_guid: list(old_stored)},
        }
        ability = SimpleNamespace(
            ordered_effects=effects,
            activation=SimpleNamespace(target_map=target_map))
        context = EffectContext.from_rules_port(
            _Game(), _Session(), _DB(), object(), "player", "ai", bstate,
            "battle-effect", ability=ability)
        context.template_value = lambda _name, default=None: True
        context.damage = lambda uid, amount: (
            damage.append((int(bstate["resolving_source_uid"]),
                           int(uid), int(amount))) or "applied")
        context._emit_authored_event = lambda event_type, target: (
            damage_events.append((event_type,
                                  int(bstate["resolving_source_uid"]),
                                  int(target))) or "queued")
        return dispatch("Battle2CardsAbilityEffectTemplate",
                        context, battle_effect)

    damage_events = []
    with mock.patch("rules_port.static_rules.effective_stats",
                    side_effect=lambda _db, _session, _state, uid:
                    (attack[int(uid)], 0)):
        resolve(first_source, first_target, [first_source])
        resolve(second_source, second_target,
                [first_source, second_source])

    assert damage == [
        (first_source, first_target, attack[first_source]),
        (first_target, first_source, attack[first_target]),
        (second_source, second_target, attack[second_source]),
        (second_target, second_source, attack[second_target]),
    ], damage
    assert damage_events == [
        ("CardBattledEvent", first_source, first_target),
        ("CardBattledEvent", first_target, first_source),
        ("CardBattledEvent", second_source, second_target),
        ("CardBattledEvent", second_target, second_source),
    ], damage_events


if __name__ == "__main__":
    test_legacy_effect_context_rejects_rules_port_sessions()
    test_native_grant_ability_can_target_a_champion()
    test_player_target_template_uses_champion_identity_for_grants()
    test_context_decorator_adapts_legacy_leaf_arguments()
    test_tunnel_moves_source_to_underground_and_emits_zone_triggers()
    test_tunneling_metadata_and_underground_visibility_are_data_driven()
    test_tunneling_surface_queue_is_empty_without_underground_cards()
    test_queue_surfaces_registers_the_native_chain_item()
    test_resource_choice_ability_is_detected_from_metadata()
    test_packet_visibility_matches_csharp_collections_and_reveal_scopes()
    test_subterranean_spy_visibility_reveals_only_the_controller_view()
    test_match_secondary_reveal_uses_stored_target_owner()
    test_player_updated_carries_subterranean_spy_hand_permission()
    test_player_updated_preserves_the_established_deck_sleeve()
    test_all_registered_effects_use_the_context_adapter()
    test_context_draw_preserves_owner_and_handler_boundary()
    test_context_authored_events_use_typed_event_names()
    test_native_transform_uses_typed_stored_target_copy()
    test_native_same_collection_transform_emits_no_wire_card_moved()
    test_persisted_original_gems_survive_transform_and_revert()
    test_native_transform_event_uses_pre_transform_viewer_visibility()
    test_card_transform_reset_and_category_gates_match_csharp()
    test_copy_transform_copies_supported_csharp_mutable_card_fields()
    test_native_transform_to_target_preserves_source_collection()
    test_context_draw_effect_uses_typed_or_fixture_count()
    test_native_draw_routes_empty_deck_to_explicit_result_projection()
    test_card_zone_projection_uses_client_derived_moves()
    test_context_randomize_variable_uses_typed_bounds()
    test_context_stat_mod_uses_shared_operation_boundary()
    test_context_damage_modifier_resolves_typed_input_and_target()
    test_context_stat_modifier_resolves_typed_input_and_duration()
    test_context_counter_hides_card_persistence_and_projection()
    test_context_counter_uses_champion_state_and_event_projection()
    test_rules_port_counter_projection_reads_persisted_buffs()
    test_context_remove_from_combat_preserves_state_event_boundary()
    test_context_card_state_owns_projection_and_tap_trigger()
    test_context_lose_thresholds_preserves_state_and_event_operation()
    test_context_typed_hero_health_and_spell_points_share_owner_projection()
    test_context_typed_card_threshold_and_subtype_preserve_metadata_values()
    test_context_modifier_keeps_deck_cards_nulled()
    test_context_typed_damage_shield_uses_client_flags()
    test_builder_reuses_metadata_cost_target_filter_and_ordering()
    test_builder_exposes_typed_values_conditions_and_continuations()
    test_builder_target_and_cost_candidates_delegate_to_shared_legality()
    test_battle2cards_uses_authored_secondary_effect_target()
    print("PASS effect context/builder tests")
