"""Boundary adapters between the Python HConnect/session host and rules port."""

from __future__ import annotations

import game_engine

from .session import (AuthoritativeSession, GameEngineEventSink,
                      SQLiteRulesSnapshot)
from .pvp_session import PvpAuthoritativeSession
from .runtime_adapter import (SQLiteCardMutationAdapter,
                              attach_pvp_runtime_facts)


def session_from_persisted_game(game_session, game: game_engine.Game, *,
                                 mutation_adapter=None, event_observer=None) -> AuthoritativeSession:
    """Construct an opt-in authoritative rules host from a ``GameSession`` row.

    ``GameSession._persist`` is already implemented through the shared
    ``pvp_db`` persistence facade.  Passing it to :class:`SQLiteRulesSnapshot`
    therefore keeps one database ownership path and retains reconnect state.
    The adapter is the authoritative scheduler for an attached live session;
    compatibility code observes the same battle checkpoint.
    """
    player_ids = tuple(player_id for player_id, _position in game_session.players)
    if not player_ids:
        # Test/local game construction may precede DB player registration.
        player_ids = (game.player_uid, game.ai_uid)
    port = AuthoritativeSession(
        game_session.session_id, player_ids,
        seed_z=int(game_session.seed_z), seed_w=int(game_session.seed_w),
        event_sink=GameEngineEventSink(game,
                                       mutation_adapter=mutation_adapter,
                                       event_observer=event_observer),
        snapshot=SQLiteRulesSnapshot(game_session),
    )
    # Restore only the port-owned scheduler values.  Legacy turn_order fields
    # remain untouched, and no synthetic action/UI events are emitted here.
    port.restore_snapshot(port.snapshot_store.load())
    return port


def pvp_session_from_persisted_game(
        game_session, game: game_engine.Game, *,
        mutation_adapter=None, event_observer=None) -> PvpAuthoritativeSession:
    """Construct the generic RulesPort host for a two-human PvP session."""
    player_ids = tuple(player_id for player_id, _position in game_session.players)
    if not player_ids:
        player_ids = (game.player_uid, game.ai_uid)
    port = PvpAuthoritativeSession(
        game_session.session_id, player_ids,
        seed_z=int(game_session.seed_z), seed_w=int(game_session.seed_w),
        event_sink=GameEngineEventSink(game,
                                       mutation_adapter=mutation_adapter,
                                       event_observer=event_observer),
        snapshot=SQLiteRulesSnapshot(game_session),
    )
    from .persistence import load_state
    port.sync_from_pvp_state(load_state(game_session))
    port.restore_snapshot(port.snapshot_store.load())
    return port


def rules_session_for(game_session, game: game_engine.Game) -> AuthoritativeSession:
    """Return the cached opt-in rules host for a live ``GameSession``.

    HConnect handlers are long-lived objects, while ``find_session_*`` may
    materialize a fresh persistence wrapper on reconnect.  Caching the port on
    that wrapper prevents duplicate action stacks during one connection, while
    the factory above remains the explicit rehydration path for a new wrapper.
    """
    cached = getattr(game_session, "_rules_port_session", None)
    if isinstance(cached, AuthoritativeSession):
        return cached
    from .persistence import load_state
    factory = (pvp_session_from_persisted_game
               if (load_state(game_session) or {}).get("pvp")
               else session_from_persisted_game)
    port = factory(game_session, game)
    setattr(game_session, "_rules_port_session", port)
    return port


def enable_rules_port(game_session, game: game_engine.Game, battle_state: dict,
                      *, pvp_api=None, event_observer=None,
                      turn_start_resolver=None,
                      **validators) -> AuthoritativeSession:
    """Construct and cache a fully wired opt-in Python rules host.

    This is the mode-integration seam: one call attaches SQLite card movement,
    runtime facts, and optional play/target validators while preserving the
    session wrapper used by the transport.
    """
    # Native scheduler and compatibility host share one mutable checkpoint
    # after attachment; the snapshot adapter stores its scheduler state in
    # this same persisted dictionary.
    if isinstance(battle_state, dict):
        # Mark the shared state explicitly so lower-level projections can
        # reject an accidental legacy rules fallback instead of silently
        # producing a hybrid result for an attached live session.
        battle_state["_rules_port_attached"] = True
        setattr(game_session, "_rules_port_battle_state", battle_state)
    cached = getattr(game_session, "_rules_port_session", None)
    if isinstance(cached, AuthoritativeSession):
        if cached.event_sink.mutation_adapter is None:
            cached.event_sink.mutation_adapter = SQLiteCardMutationAdapter(
                game_session.session_id, pvp_api=pvp_api)
        elif cached.event_sink is not None:
            # Reconnects may supply a replacement mutable checkpoint. Keep
            # the cached native facts bridge on that same object so every
            # requirement observes current resources, phases, and card state.
            cached.event_sink.game = game
        if cached.runtime_facts is not None:
            cached.runtime_facts.battle_state = battle_state
            cached.runtime_facts.player_uid = game.player_uid
            cached.runtime_facts.ai_uid = game.ai_uid
        if cached.runtime_facts is None:
            attach_pvp_runtime_facts(
                cached, game_session, battle_state,
                player_uid=game.player_uid, ai_uid=game.ai_uid,
                pvp_api=pvp_api, **validators)
        if turn_start_resolver is not None:
            cached.set_turn_start_resolver(turn_start_resolver)
        return cached
    mutation = SQLiteCardMutationAdapter(game_session.session_id,
                                         pvp_api=pvp_api)
    port = session_from_persisted_game(
        game_session, game, mutation_adapter=mutation,
        event_observer=event_observer)
    attach_pvp_runtime_facts(
        port, game_session, battle_state,
        player_uid=game.player_uid, ai_uid=game.ai_uid,
        pvp_api=pvp_api, **validators)
    port.set_turn_start_resolver(turn_start_resolver)
    setattr(game_session, "_rules_port_session", port)
    return port
