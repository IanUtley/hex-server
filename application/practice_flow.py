"""Practice/PvE mulligan and game-start pushes, and chain/UID helpers for the session path."""

import game_engine


def _push_pve_mulligan_start(game, player_uid, ai_uid,
                            play_first_is_player, resolve_ai_mulligan):
    """Project C#'s active-player mulligan sequence for Practice/PvE.

    If the AI plays first, it resolves its mulligan before priority passes to
    the human. A human redraw keeps the active phase and is handled by the
    transaction path without calling this setup transition again.
    """
    first_uid = player_uid if play_first_is_player else ai_uid
    game.push_green_light(first_uid, game_engine.EPriorityContext.Normal)
    game.push_turn_phase(game_engine.ETurnPhases.Mulligan,
                         first_uid, first_uid)
    if play_first_is_player:
        return False
    resolve_ai_mulligan()
    game.push_green_light(player_uid, game_engine.EPriorityContext.Normal)
    game.push_turn_phase(game_engine.ETurnPhases.Mulligan,
                         player_uid, player_uid)
    return True


def _push_pve_game_started(game, *, player_name, ai_name,
                           player_champion_guid, ai_champion_guid,
                           player_sleeve_guid, ai_sleeve_guid,
                           coin_winner_is_player):
    """Publish a normal PvE toss result through the C# GameStarted contract.

    The coin winner is the first UID in class-1 ``TurnOrder``. Class 60 is
    reserved for a forced first player: Unity's ``OnFirstPlayerDictated`` sets
    ``m_CoinFlipSkip``, which skips the visible toss animation.
    """
    game.push_game_started(
        champion_names=[player_name, ai_name],
        champion_template_ids=[player_champion_guid, ai_champion_guid],
        player_first=coin_winner_is_player,
        sleeve_template_ids=[player_sleeve_guid, ai_sleeve_guid])


def _same_uid(left, right):
    """Compare participant IDs across typed-UID and persisted-uint64 forms."""
    if left is None or right is None:
        return left is right
    try:
        return int(getattr(left, "uid64", left)) == int(
            getattr(right, "uid64", right))
    except (TypeError, ValueError):
        return left == right


def _uid_in(value, participants):
    """Return whether a raw or typed UID belongs to a participant list."""
    return any(_same_uid(value, participant) for participant in participants)


def _is_practice_chain_follow_up(action, client_id, practice_window):
    """Return whether a server-owned chain response should be auto-passed.

    A completed response can expose the AI's next pass on the same ability
    chain.  An ordinary phase transition can also expose an AI-owned priority
    action, notably when DeclareDefensePriorityWindow enters AssignDamage.
    Only the former belongs to this auto-pass path; phase actions must return
    to the AI driver so phase-specific work such as combat damage runs first.
    """
    from rules_port.kernel import PriorityWindowAction
    return bool(
        practice_window and
        isinstance(action, PriorityWindowAction) and
        getattr(action, "ability_responding_to", None) is not None and
        action.priority_player_id is not None and
        not _same_uid(action.priority_player_id, client_id))


def _target_count_from_text(game_text):
    """Parse how many targets a "X troops you control" cost/target template
    needs: "a troop" = 1, "two/three/... troops" = the number. Defaults to 1."""
    import re as _re
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
             "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
    m = _re.search(r'\b(one|two|three|four|five|six|seven|eight|nine|ten)\b', (game_text or "").lower())
    return words[m.group(1)] if m else 1
