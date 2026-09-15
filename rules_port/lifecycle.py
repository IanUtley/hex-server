"""Native RulesPort turn-boundary state transitions."""

from __future__ import annotations

import json

import game_engine
from .chain import (empty as stack_empty, pop as stack_pop,
                    set_pass as stack_set_pass,
                    both_passed as stack_both_passed,
                    reset_passes as stack_reset_passes)
from .persistence import (current_phase, load_state, save_state)


PLAYER = "player"
AI = "ai"
BASE_TURN_PHASES = [
    game_engine.ETurnPhases.StartTurn,
    game_engine.ETurnPhases.Ready,
    game_engine.ETurnPhases.Prep,
    game_engine.ETurnPhases.Draw,
    game_engine.ETurnPhases.FirstMainPhase,
    game_engine.ETurnPhases.SecondMainPhase,
    game_engine.ETurnPhases.EndPhase,
    game_engine.ETurnPhases.Discard,
    game_engine.ETurnPhases.EndTurn,
]
TURN_PHASES = BASE_TURN_PHASES
COMBAT_STEPS = [
    game_engine.ETurnPhases.DeclareCombatPriorityWindow,
    game_engine.ETurnPhases.DeclareAttack,
    game_engine.ETurnPhases.DeclareAttackPriorityWindow,
    game_engine.ETurnPhases.DeclareDefense,
    game_engine.ETurnPhases.DeclareDefensePriorityWindow,
    game_engine.ETurnPhases.AssignFirstStrikeDamage,
    game_engine.ETurnPhases.FirstStrikePriorityWindow,
    game_engine.ETurnPhases.AssignDamage,
]

# Shared client stop policy used by both the ordinary RulesPort session and
# the tournament projection.  The tournament state has its own ``phase`` /
# player-id schema, so it consumes these constants without using this module's
# load/save helpers.
COMBAT_TURN_PHASES = [
    game_engine.ETurnPhases.StartTurn,
    game_engine.ETurnPhases.Ready,
    game_engine.ETurnPhases.Prep,
    game_engine.ETurnPhases.Draw,
    game_engine.ETurnPhases.FirstMainPhase,
] + COMBAT_STEPS + [
    game_engine.ETurnPhases.SecondMainPhase,
    game_engine.ETurnPhases.EndPhase,
    game_engine.ETurnPhases.Discard,
    game_engine.ETurnPhases.EndTurn,
]

SELF_ALWAYS_STOPS = {
    game_engine.ETurnPhases.PickGoesFirst,
    game_engine.ETurnPhases.Mulligan,
    game_engine.ETurnPhases.StartGame,
    game_engine.ETurnPhases.DeclareAttack,
    game_engine.ETurnPhases.AssignDamage,
    game_engine.ETurnPhases.AssignFirstStrikeDamage,
}
OPP_ALWAYS_STOPS = {game_engine.ETurnPhases.DeclareDefense}
SELF_DEFAULT_STOPS = {
    game_engine.ETurnPhases.FirstMainPhase,
    game_engine.ETurnPhases.SecondMainPhase,
    game_engine.ETurnPhases.DeclareCombatPriorityWindow,
    game_engine.ETurnPhases.DeclareAttackPriorityWindow,
    game_engine.ETurnPhases.DeclareDefensePriorityWindow,
}
OPP_DEFAULT_STOPS = {
    game_engine.ETurnPhases.SecondMainPhase,
    game_engine.ETurnPhases.DeclareAttackPriorityWindow,
    game_engine.ETurnPhases.DeclareDefensePriorityWindow,
}

# Compatibility names for the tournament projection's separately persisted
# PvP state.  They are state-neutral mutations and do not load the legacy
# battle engine.
from .chain import (push as stack_push, pop as stack_pop,
                    empty as stack_empty, top as stack_top,
                    clear as stack_clear)


def default_state(turn_player=PLAYER):
    """Create the shared checkpoint used when a native game starts."""
    return {
        "turn_player": turn_player,
        "turn_number": 1,
        "phase_idx": 0,
        "player_passed": False,
        "ai_passed": False,
        "player_resources": 0,
        "player_total_resources": 0,
        "player_threshold": {},
        "ai_resources": 0,
        "ai_total_resources": 0,
        "ai_threshold": {},
        "player_resource_played_this_turn": False,
        "ai_resource_played_this_turn": False,
        "player_charges": 0,
        "ai_charges": 0,
        "player_spell_points": 0,
        "ai_spell_points": 0,
        "player_sp_uses": {},
        "player_self_stops": None,
        "player_opp_stops": None,
        "player_health": 20,
        "ai_health": 20,
        "player_attackers": {},
        "player_has_ready_troop": False,
        "turn_phases": list(BASE_TURN_PHASES),
        "resolve_counter": 0,
    }


def build_turn_phases(state):
    """Build the persisted phase list from native combat facts."""
    phases = (BASE_TURN_PHASES if not state.get("player_has_ready_troop")
              else BASE_TURN_PHASES[:5] + COMBAT_STEPS + BASE_TURN_PHASES[5:])
    entries = (state.get("extra_combats_this_turn") or {}).get(PLAYER, [])
    if not entries:
        return phases
    try:
        second_main = phases.index(game_engine.ETurnPhases.SecondMainPhase)
    except ValueError:
        return phases
    return (phases[:second_main + 1] +
            sum((COMBAT_STEPS + [game_engine.ETurnPhases.SecondMainPhase]
                 for _entry in entries), []) + phases[second_main + 1:])


def next_turn_player(state):
    current = state.get("turn_player")
    if state.get("bonus_turn") == current:
        state.pop("bonus_turn", None)
        return current
    return AI if current == PLAYER else PLAYER


def complete_turn(state):
    """Apply the native end-turn checkpoint transition and return its owner.

    End-of-turn effects and card/event projection stay with the mode adapter;
    this function owns only the shared scheduler state consumed by both PvE
    and PvP hosts.
    """
    if not isinstance(state, dict):
        return None
    next_player = next_turn_player(state)
    state["turn_player"] = next_player
    state["turn_number"] = int(state.get("turn_number", 1) or 1) + 1
    state["phase_idx"] = 0
    state["turn_phases"] = list(BASE_TURN_PHASES)
    state["player_passed"] = False
    state["ai_passed"] = False
    state.pop("ai_turn_phase_idx", None)
    state.pop("ai_attackers", None)
    state.pop("ai_blockers", None)
    state.pop("player_attackers", None)
    state.pop("player_damage_order", None)
    state[f"{next_player}_resource_played_this_turn"] = False
    return next_player


def turn_phases(state):
    phases = state.get("turn_phases")
    if not phases:
        phases = build_turn_phases(state)
        state["turn_phases"] = phases
    return phases


def skip_to_phase(state, phase):
    phases = turn_phases(state)
    try:
        target = phases.index(phase, int(state.get("phase_idx", 0) or 0))
    except (TypeError, ValueError):
        return False
    state["phase_idx"] = target
    return True


def is_self_stop(state, phase):
    always = {
        game_engine.ETurnPhases.PickGoesFirst,
        game_engine.ETurnPhases.Mulligan,
        game_engine.ETurnPhases.StartGame,
        game_engine.ETurnPhases.DeclareAttack,
        game_engine.ETurnPhases.AssignDamage,
        game_engine.ETurnPhases.AssignFirstStrikeDamage,
    }
    defaults = {
        game_engine.ETurnPhases.FirstMainPhase,
        game_engine.ETurnPhases.SecondMainPhase,
        game_engine.ETurnPhases.DeclareCombatPriorityWindow,
        game_engine.ETurnPhases.DeclareAttackPriorityWindow,
        game_engine.ETurnPhases.DeclareDefensePriorityWindow,
    }
    configured = state.get("player_self_stops")
    stops = defaults if configured is None else set(configured)
    return phase in always or phase in stops


def is_opp_stop(state, phase):
    """Return whether the human configured an opponent-turn stop."""
    always = {game_engine.ETurnPhases.DeclareDefense}
    defaults = {
        game_engine.ETurnPhases.SecondMainPhase,
        game_engine.ETurnPhases.DeclareAttackPriorityWindow,
        game_engine.ETurnPhases.DeclareDefensePriorityWindow,
    }
    configured = state.get("player_opp_stops")
    stops = defaults if configured is None else set(configured)
    return phase in always or phase in stops


def practice_priority_players(state, phase, *, active_is_player):
    """Return the native priority-window kind for a Practice/PvE phase.

    Practice has one wire client and one server-driven participant, but it
    still uses the same stop semantics as a two-player session.  When both
    players stop in the active player's phase the native queue is ``ALL``:
    the active player passes, then the opponent passes internally, and only
    then may RulesPort advance the phase.
    """
    from .kernel import TurnPhasePlayers

    if active_is_player:
        self_stop = is_self_stop(state, phase)
        opponent_stop = is_opp_stop(state, phase)
        if self_stop and opponent_stop:
            return TurnPhasePlayers.ALL
        if self_stop:
            return TurnPhasePlayers.ACTIVE
        if opponent_stop:
            return TurnPhasePlayers.ALL
        return TurnPhasePlayers.NONE

    # DeclareDefense is the one phase whose C# action deliberately queues the
    # defending player rather than APNAP/all-player priority. All ordinary
    # opponent stops use ALL so the AI can pass through its native queue.
    if phase == game_engine.ETurnPhases.DeclareDefense:
        return TurnPhasePlayers.DEFENDING
    # The server AI still needs a native priority turn to play resources and
    # cards.  A missing opponent stop only means the human should not be
    # shown that AI window; it does not mean the AI phase is NONE.  With an
    # opponent stop, ALL lets the AI act first and then exposes the human
    # response window.
    return (TurnPhasePlayers.ALL if is_opp_stop(state, phase)
            else TurnPhasePlayers.ACTIVE)


def practice_phase_priority(state, phase, *, active_player_id,
                            player_id):
    """Resolve Practice's native window policy across persisted UID forms.

    Practice checkpoints historically persist participant IDs as uint64s,
    while the live Game projection uses ``UID`` objects.  Keep that identity
    normalization beside the stop matrix so the HConnect adapter cannot
    accidentally classify the human's own phase as an opponent phase.
    """
    def uid_value(value):
        try:
            return int(getattr(value, "uid64", value))
        except (TypeError, ValueError):
            return None

    active_value = uid_value(active_player_id)
    player_value = uid_value(player_id)
    active_is_player = (
        active_value is not None and player_value is not None and
        active_value == player_value)
    return practice_priority_players(
        state, phase, active_is_player=active_is_player)


def ai_held_phase_context(state):
    """Return the native phase represented by a paused AI resume cursor."""
    if not isinstance(state, dict) or state.get("ai_turn_phase_idx") is None:
        return None
    try:
        index = int(state["ai_turn_phase_idx"]) - 1
    except (TypeError, ValueError):
        return None
    phases = state.get("turn_phases") or ()
    if 0 <= index < len(phases):
        return phases[index], index, phases
    return None


_EXPIRATIONS = "__attribute_expirations"


def advance_checkpoint_phase(state):
    """Advance the shared phase cursor for a native live-session projection.

    The scheduler owns the typed phase graph; this small adapter updates the
    legacy-compatible checkpoint consumed by the host's event projection.
    """
    if not isinstance(state, dict):
        return None
    state["player_passed"] = False
    state["ai_passed"] = False
    phases = state.get("turn_phases") or ()
    if not phases:
        return state.get("phase")
    try:
        index = int(state.get("phase_idx", 0) or 0) + 1
    except (TypeError, ValueError):
        index = 1
    if index >= len(phases):
        current = state.get("turn_player", "player")
        state["turn_player"] = "ai" if current == "player" else "player"
        state["turn_number"] = int(state.get("turn_number", 1) or 1) + 1
        state["phase_idx"] = 0
        state.pop("extra_combats_this_turn", None)
    else:
        state["phase_idx"] = index
    return phases[int(state["phase_idx"])]


def should_draw_for_turn(state, side):
    """Return whether the active side draws in its current Draw phase."""
    if int((state or {}).get("turn_number", 1) or 1) > 1:
        return True
    first_player_draws = bool((state or {}).get("player_draws_first_turn"))
    return first_player_draws if str(side).lower() == "player" else not first_player_draws


# Names match the old checkpoint helper so host projection code can select
# this module for attached sessions without changing its control-flow shape.
advance_phase = advance_checkpoint_phase


def prime_cards_for_turn(db, session_id, active_owner_id):
    """Apply the port-owned ResetActiveCards/PrimeCard transition."""
    from pvp_db import db_warzone_display_rows, db_set_card_state_exact
    prime_mask = (
        game_engine.ECardStates.Blocking |
        game_engine.ECardStates.Attacking |
        game_engine.ECardStates.Damaged |
        game_engine.ECardStates.Healed |
        game_engine.ECardStates.HasAttacked |
        game_engine.ECardStates.HasBlocked |
        game_engine.ECardStates.ZoneChangeReplacement |
        game_engine.ECardStates.Activated |
        game_engine.ECardStates.VoidsIfDestroyed |
        game_engine.ECardStates.CameOutThisTurn |
        game_engine.ECardStates.StartedATurnOnYourSide)
    changed = []
    for card_uid, template_guid, owner_id, old_state, card_type in \
            db_warzone_display_rows(session_id, conn=db):
        old_state = int(old_state or 0)
        new_state = old_state & ~game_engine.ECardStates.CameOutThisTurn
        if int(owner_id or 0) == int(active_owner_id or 0):
            new_state = ((new_state & ~prime_mask) |
                         game_engine.ECardStates.StartedATurnOnYourSide)
        if new_state == old_state:
            continue
        db_set_card_state_exact(session_id, int(card_uid), new_state, conn=db)
        changed.append((int(card_uid), template_guid, int(owner_id or 0),
                        new_state, card_type))
    db.commit()
    return changed


def ready_cards_for_turn(db, session_id, owner_id):
    """Apply the port-owned Prep readiness transition for one controller."""
    from pvp_db import (db_warzone_cards_with_state,
                        db_warzone_card_state_attributes,
                        db_reset_warzone_troop)
    clear_mask = (game_engine.ECardStates.Tapped |
                  game_engine.ECardStates.Attacking |
                  game_engine.ECardStates.HasAttacked |
                  game_engine.ECardStates.Blocking |
                  game_engine.ECardStates.HasBlocked)
    changed = []
    for card_uid, template_guid, card_owner, card_state in \
            db_warzone_cards_with_state(session_id, conn=db):
        if int(card_owner or 0) != int(owner_id or 0):
            continue
        previous = int(card_state or 0)
        attrs_row = db_warzone_card_state_attributes(
            session_id, int(card_uid), conn=db)
        attrs = attrs_row[1] if attrs_row else 0
        mask = clear_mask
        if attrs and int(attrs) & game_engine.ECardAttributes.CantReadyAutomatically:
            mask &= ~game_engine.ECardStates.Tapped
        db_reset_warzone_troop(session_id, int(card_uid), mask, conn=db)
        new_state = previous & ~mask
        if new_state != previous:
            changed.append((int(card_uid), template_guid, int(card_owner or 0),
                            previous, new_state))
    db.commit()
    return changed


def clear_combat_damage(db, session_id):
    from pvp_db import db_clear_warzone_damage
    db_clear_warzone_damage(session_id, conn=db)
    db.commit()


def clear_expired_temporary_attributes(db, session_id, owner_id, boundary,
                                       clear_stat_buffs=False):
    """Expire temporary attributes and stat grants at an authored boundary."""
    from pvp_db import (db_temporary_attribute_rows,
                        db_set_temporary_card_state)
    changed = []
    for card_uid, target_owner, attrs, raw_buffs in db_temporary_attribute_rows(
            session_id, conn=db):
        try:
            buffs = json.loads(raw_buffs or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            buffs = {}
        if not isinstance(buffs, dict):
            buffs = {}
        metadata = buffs.get(_EXPIRATIONS) or {}
        expired_bits = 0
        remaining = {}
        for bit_text, rule in metadata.items():
            try:
                bit = int(bit_text)
            except (TypeError, ValueError):
                continue
            if (isinstance(rule, dict) and
                    int(rule.get("owner", -1)) == int(owner_id) and
                    rule.get("boundary") == boundary):
                expired_bits |= bit
            else:
                remaining[bit_text] = rule
        if not metadata and int(target_owner or 0) == int(owner_id):
            expired_bits = int(attrs or 0)
        new_attrs = int(attrs or 0) & ~expired_bits
        if remaining:
            buffs[_EXPIRATIONS] = remaining
        else:
            buffs.pop(_EXPIRATIONS, None)
        if clear_stat_buffs:
            buffs = {key: value for key, value in buffs.items()
                     if key == _EXPIRATIONS}
        new_buffs = json.dumps(buffs, separators=(",", ":"), sort_keys=True)
        if (new_attrs == int(attrs or 0) and
                new_buffs == (raw_buffs or "{}")):
            continue
        db_set_temporary_card_state(
            session_id, int(card_uid), new_attrs, new_buffs, conn=db)
        changed.append(int(card_uid))
    if changed:
        db.commit()
    return changed
