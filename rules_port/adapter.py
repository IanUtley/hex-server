"""Boundary adapters between the Python HConnect/session host and rules port."""

from __future__ import annotations

import game_engine
from domain.constants import AI_UID_TYPE

from .session import (AuthoritativeSession, GameEngineEventSink,
                      SQLiteRulesSnapshot)
from .kernel import PriorityWindowAction
from .pvp_session import PvpAuthoritativeSession
from .runtime_adapter import (SQLiteCardMutationAdapter,
                              attach_pvp_runtime_facts)

PRACTICE_AI_INSTANCE_ID = 1000


def _native_participant_ids(game_session, game: game_engine.Game):
    """Return the typed participants used by the native scheduler.

    Practice/PvE sessions historically persist only the human's raw
    Reckoning id (and some older rows contain that id twice).  The wire Game
    still has the typed human and AI UIDs.  RulesPort must use one identity
    domain for queue membership and transaction validation, so repair that
    compatibility shape at the adapter boundary instead of making every
    phase/action compare raw and typed IDs.
    """
    stored = tuple(getattr(game_session, "players", ()) or ())
    # Practice's server actor has a fixed SessionPlayer identity. Some setup
    # and reconnect projections temporarily carry the human in both Game UID
    # fields. Deriving participants from that transient projection shrinks the
    # native tuple to one human, so EndTurn rotates modulo one forever.
    is_practice = str(
        getattr(game_session, "session_name", "") or "").startswith(
            "Session-")
    practice_ai = (game_engine.UID.make(
        AI_UID_TYPE, PRACTICE_AI_INSTANCE_ID)
        if is_practice else game.ai_uid)
    if not stored:
        return tuple(value for value in (game.player_uid, practice_ai)
                     if value is not None)

    typed = []
    for position, entry in enumerate(stored):
        value = entry[0] if isinstance(entry, (tuple, list)) else entry
        # JSON-loaded GameSession rows contain integer raw IDs.  Position 0 is
        # the human and position 1 is the server AI, even when an old practice
        # row duplicated the human value for both entries.
        if isinstance(value, int) and position == 0:
            value = game.player_uid
        elif isinstance(value, int) and position == 1:
            value = practice_ai
        if value is not None and value not in typed:
            typed.append(value)

    # A normal practice row has one human entry.  It is still a two-player
    # RulesPort session: append the typed AI participant for native APNAP and
    # turn rotation.
    if practice_ai not in typed:
        typed.append(practice_ai)
    if game.player_uid is not None and game.player_uid not in typed:
        typed.insert(0, game.player_uid)
    return tuple(typed)


def _resolve_cached_participant(value, expected, game):
    """Map a legacy cached participant onto the current typed UID.

    A live handler can retain a RulesPort created while the persisted practice
    row still contained a raw Reckoning id.  Raw and typed UIDs are different
    integer values, so equality alone cannot repair the cached active/priority
    owner during a reconnect or the first AI turn.
    """
    if value is None:
        return None
    if value in expected:
        return value
    try:
        raw = int(getattr(value, "uid64", value))
    except (TypeError, ValueError):
        return None
    for candidate in expected:
        if raw == int(getattr(candidate, "uid64", candidate)):
            return candidate
    for candidate in (game.player_uid, game.ai_uid):
        if candidate is None or candidate not in expected:
            continue
        instance = int(getattr(candidate, "instance_id", 0))
        if raw == instance or (raw == 0 and candidate == game.ai_uid):
            return candidate
    return None


def _align_cached_participants(game_session, game, port):
    """Repair a cached practice host created from legacy player rows."""
    if isinstance(port, PvpAuthoritativeSession):
        return
    expected = _native_participant_ids(game_session, game)
    if not expected or tuple(port.player_ids) == expected:
        return

    old_active = port.active_player_id
    old_priority = port.action_stack.priority_player_id
    port.player_ids = expected
    port.active_player_id = (
        _resolve_cached_participant(old_active, expected, game)
        or game.player_uid or expected[0])
    port.action_stack.priority_player_id = _resolve_cached_participant(
        old_priority, expected, game)

    # Preserve a waiting phase action, but move its queue into the same typed
    # identity domain before sync_checkpoint applies the current stop policy.
    action = port.action_stack.peek()
    if isinstance(action, PriorityWindowAction):
        from collections import deque
        queue = []
        for player in tuple(getattr(action, "_priority_queue", ())):
            resolved = _resolve_cached_participant(player, expected, game)
            if resolved is not None and resolved not in queue:
                queue.append(resolved)
        action._priority_queue = deque(queue)
        # priority_player_id is a read-only view over _priority_queue.
        port.action_stack.priority_player_id = action.priority_player_id


def session_from_persisted_game(game_session, game: game_engine.Game, *,
                                 mutation_adapter=None, event_observer=None) -> AuthoritativeSession:
    """Construct an opt-in authoritative rules host from a ``GameSession`` row.

    ``GameSession._persist`` is already implemented through the shared
    ``pvp_db`` persistence facade.  Passing it to :class:`SQLiteRulesSnapshot`
    therefore keeps one database ownership path and retains reconnect state.
    The adapter is the authoritative scheduler for an attached live session;
    compatibility code observes the same battle checkpoint.
    """
    player_ids = _native_participant_ids(game_session, game)
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
    cached = getattr(game_session, "_rules_port_session", None)
    if isinstance(cached, AuthoritativeSession):
        # Re-entrant projections can call enable_rules_port while a native
        # phase transition is still on the stack.  Their ``battle_state`` is
        # the compatibility snapshot captured before that transition.  Do
        # not replace the shared checkpoint (and its newly selected turn
        # owner) with that stale dictionary.  Refresh the caller's object in
        # place so downstream projection code also observes the native state.
        shared = getattr(game_session, "_rules_port_battle_state", None)
        if isinstance(shared, dict) and shared is not battle_state:
            if isinstance(battle_state, dict):
                battle_state.clear()
                battle_state.update(shared)
        elif isinstance(battle_state, dict):
            shared = battle_state
            setattr(game_session, "_rules_port_battle_state", shared)
        if isinstance(shared, dict):
            shared["_rules_port_attached"] = True
        _align_cached_participants(game_session, game, cached)
        if cached.event_sink.mutation_adapter is None:
            cached.event_sink.mutation_adapter = SQLiteCardMutationAdapter(
                game_session.session_id, pvp_api=pvp_api)
        elif cached.event_sink is not None:
            # Reconnects may supply a replacement mutable checkpoint. Keep
            # the cached native facts bridge on that same object so every
            # requirement observes current resources, phases, and card state.
            cached.event_sink.game = game
        if cached.runtime_facts is not None:
            cached.runtime_facts.battle_state = (
                shared if isinstance(shared, dict) else battle_state)
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
    # Native scheduler and compatibility host share one mutable checkpoint
    # after first attachment; the snapshot adapter stores its scheduler state
    # in this same persisted dictionary.
    if isinstance(battle_state, dict):
        # Mark the shared state explicitly so lower-level projections can
        # reject an accidental legacy rules fallback instead of silently
        # producing a hybrid result for an attached live session.
        battle_state["_rules_port_attached"] = True
        setattr(game_session, "_rules_port_battle_state", battle_state)
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
