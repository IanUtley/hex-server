"""Metadata-driven player visibility projections.

Some continuous card modifiers target a player rather than a card.  The
client represents those permissions as PlayerUpdated flags, while the server
must also project the newly visible cards into the recipient's packet.
"""

import json

import game_engine

from gamedata import DEFAULT_RECORD_STORE, ability_graph

from .._shared import owner_uid
from ..condition_engine import ConditionContext, evaluate_effect_condition
from ..fields import modifier_metadata


_HAND_VISIBILITY_ATTR = "canseeopponentshand"


def _json_list(value):
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _champions(handler):
    callback = getattr(handler, "_champion_targets", None)
    if not callable(callback):
        return []
    try:
        return callback() or []
    except Exception:
        return []


def _has_active_player_modifier(db, session, handler, pl_t, ai_t, bstate,
                                card_uid, owner_id, ability_guid, attr):
    graph = ability_graph(DEFAULT_RECORD_STORE, str(ability_guid).lower())
    if graph is None:
        return False
    champions = _champions(handler)
    for effect in graph.effects:
        meta = modifier_metadata(effect.guid)
        if (effect.concrete_type != "CardModifierAbilityEffectTemplate"
                or meta.get("property") != "intattr"
                or str(meta.get("attribute") or "").lower() != attr):
            continue
        if effect.target_index < 0 or effect.target_index >= len(graph.targets):
            continue
        if graph.targets[effect.target_index].target_kind != \
                "PlayerTargetTemplate":
            continue
        condition_id = str(effect.condition_guid or "")
        if condition_id:
            context_type = ConditionContext
            evaluate = evaluate_effect_condition
            if (getattr(session, "_rules_port_session", None) is not None or
                    bstate.get("_rules_port_attached")):
                # Visibility is emitted by a projection module, but its
                # authored modifier condition is still gameplay semantics.
                # Evaluate it through RulesPort for native sessions so this
                # projection cannot quietly re-enter the legacy condition
                # engine.
                from rules_port.conditions import (ConditionContext as
                                                    PortConditionContext,
                                                    evaluate_effect_condition as
                                                    evaluate_port_condition)
                context_type = PortConditionContext
                evaluate = evaluate_port_condition
            context = context_type(
                db, session, bstate,
                ability_source_uid=int(card_uid),
                ability_source_owner_id=int(owner_id),
                ability_source_card_owner=int(owner_id),
                trigger_owner_id=int(owner_id),
                pl_t=pl_t, ai_t=ai_t, champions=champions)
            if not evaluate(db, condition_id, context):
                continue
        try:
            if int(meta.get("value", meta.get("input_value", 1)) or 0) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        return True
    return False


def _visibility_flags(db, session, handler, pl_t, ai_t, bstate):
    flags = {}
    from pvp_db import db_visibility_underground_rows
    rows = db_visibility_underground_rows(session.session_id, conn=db)
    for card_uid, owner_id, abilities_json in rows:
        for ability_guid in _json_list(abilities_json):
            if _has_active_player_modifier(
                    db, session, handler, pl_t, ai_t, bstate,
                    int(card_uid), int(owner_id), ability_guid,
                    _HAND_VISIBILITY_ATTR):
                flags.setdefault(str(int(owner_id)), {})[
                    "CanSeeOpponentsHand"] = 1
    return flags


def _uid_for_owner(game, owner_id, bstate):
    return owner_uid(int(owner_id), game.player_uid, game.ai_uid, bstate)


def _opponent_owner(bstate, owner_id, handler):
    if (bstate or {}).get("pvp"):
        pids = [int(pid) for pid in (bstate.get("pids") or [])]
        if not pids:
            pids = [int(pid) for pid in (bstate.get("champ_map") or {})]
        return next((pid for pid in pids if pid != int(owner_id)), None)
    if int(owner_id) == 0:
        return int(handler.user_profile["id"]) if handler.user_profile else None
    return 0


def _push_hand_projection(db, session, handler, game, pl_t, ai_t, bstate,
                          viewer_owner, visible, card_uids=None):
    opponent_owner = _opponent_owner(bstate, viewer_owner, handler)
    if opponent_owner is None:
        return
    viewer_uid = _uid_for_owner(game, viewer_owner, bstate)
    from pvp_db import db_visibility_hand_rows
    rows = db_visibility_hand_rows(
        session.session_id, int(opponent_owner), conn=db)
    selected = ({int(uid) for uid in card_uids}
                if card_uids is not None else None)
    for card_uid, template_guid, owner_id, card_type, card_state in rows:
        if selected is not None and int(card_uid) not in selected:
            continue
        scid = game_engine.SessionCardId(game_engine.UID(int(card_uid)))
        try:
            _tpl, ct, _name, cost, attack, defense, gems = \
                handler._card_full_data(game, scid, template_guid)
        except Exception:
            continue
        game.push_card_updated(
            scid, _uid_for_owner(game, owner_id, bstate),
            game_engine.ECardCollections.Hand, ct,
            template_id=template_guid, state=int(card_state or 0),
            cost=cost, attack=attack, defense=defense, gems=gems,
            nulling=not visible)
        event = game.events[-1]
        event._hand_reveal_viewer_uid = viewer_uid


def project_visible_hands(db, session, handler, game, pl_t, ai_t, bstate):
    """Re-send visible opposing-hand definitions into a fresh Game packet.

    Visibility permissions persist in the battle snapshot, but ``Game`` is a
    per-packet event buffer.  A phase/priority packet can therefore contain a
    hidden hand update after the Spy permission was already active.  Track the
    card UIDs projected into this Game and append full ``CardUpdated`` events
    for newly visible cards (or all cards on a new Game instance).
    """
    flags = (bstate or {}).get("player_visibility") or {}
    projected = getattr(game, "_visible_hand_uids", None)
    if projected is None:
        projected = {}
        game._visible_hand_uids = projected
    active = set()
    for owner_id, attrs in flags.items():
        try:
            owner = int(owner_id)
        except (TypeError, ValueError):
            continue
        if not int((attrs or {}).get("CanSeeOpponentsHand", 0) or 0):
            continue
        opponent = _opponent_owner(bstate, owner, handler)
        if opponent is None:
            continue
        from pvp_db import db_hand_card_uids
        current = {int(row[0]) for row in db_hand_card_uids(
            session.session_id, int(opponent), conn=db)}
        key = str(owner)
        previous = projected.get(key)
        to_push = current if previous is None else current - set(previous)
        if to_push:
            _push_hand_projection(
                db, session, handler, game, pl_t, ai_t, bstate,
                owner, True, card_uids=to_push)
        projected[key] = sorted(current)
        active.add(key)
    for key in list(projected):
        if key not in active:
            projected.pop(key, None)
    return projected


def apply_player_visibility_to_game(game, bstate):
    """Project persisted DB-owner visibility flags onto a Game packet."""
    projected = {}
    for owner_id, attrs in ((bstate or {}).get("player_visibility") or {}).items():
        try:
            uid = _uid_for_owner(game, int(owner_id), bstate)
        except (TypeError, ValueError):
            continue
        # PvP state snapshots may contain primitive UID integers while
        # Practice projections use game_engine.UID wrappers. Normalize both
        # representations at the visibility boundary.
        projected[int(getattr(uid, "uid64", uid))] = dict(attrs or {})
    game._visibility_by_uid = projected


def refresh_player_visibility(db, session, handler, game, pl_t, ai_t, bstate):
    """Recalculate active player visibility modifiers from underground cards."""
    old_flags = (bstate or {}).get("player_visibility") or {}
    new_flags = _visibility_flags(db, session, handler, pl_t, ai_t, bstate)
    changed_owners = {
        str(owner) for owner in set(old_flags) | set(new_flags)
        if bool((old_flags.get(owner) or {}).get("CanSeeOpponentsHand")) !=
        bool((new_flags.get(owner) or {}).get("CanSeeOpponentsHand"))
    }
    bstate["player_visibility"] = new_flags
    apply_player_visibility_to_game(game, bstate)
    # PlayerUpdated is a client card-cache boundary. Populate the same
    # objective champion ids used by the surrounding packet before emitting
    # it; an undefined ChampionId corrupts the fixed client cache.
    if (bstate or {}).get("pvp"):
        champ_map = bstate.get("champ_map") or {}
        player_pid = int(getattr(pl_t, "uid64", pl_t) or 0) >> 8
        ai_pid = int(getattr(ai_t, "uid64", ai_t) or 0) >> 8
        player_champ = int(champ_map.get(str(player_pid), 0) or 0)
        ai_champ = int(champ_map.get(str(ai_pid), 0) or 0)
        if player_champ:
            game.player_champion_card_id = game_engine.SessionCardId(
                game_engine.UID(player_champ))
        if ai_champ:
            game.ai_champion_card_id = game_engine.SessionCardId(
                game_engine.UID(ai_champ))
    else:
        game.player_champion_card_id = getattr(
            handler, "_player_champ_scid", game.player_champion_card_id)
        game.ai_champion_card_id = getattr(
            handler, "_ai_champ_scid", game.ai_champion_card_id)
    for owner in changed_owners:
        visible = bool((new_flags.get(owner) or {}).get(
            "CanSeeOpponentsHand"))
        _push_hand_projection(
            db, session, handler, game, pl_t, ai_t, bstate,
            int(owner), visible)
        if visible:
            # The changed owner has just received a complete hand projection;
            # seed the per-Game tracker so the follow-up pass only emits new
            # cards entering that hand.
            opponent = _opponent_owner(bstate, int(owner), handler)
            if opponent is not None:
                game._visible_hand_uids = getattr(
                    game, "_visible_hand_uids", {})
                from pvp_db import db_hand_card_uids
                game._visible_hand_uids[str(owner)] = sorted(
                    int(row[0]) for row in db_hand_card_uids(
                        session.session_id, int(opponent), conn=db))
        else:
            getattr(game, "_visible_hand_uids", {}).pop(str(owner), None)
    # A permission can remain active across phase packets.  Project any hand
    # cards that were not included in the current Game (for example a card
    # drawn by the opponent after the Spy became underground).
    project_visible_hands(db, session, handler, game, pl_t, ai_t, bstate)
    for owner in set(old_flags) | set(new_flags):
        owner_uid_value = _uid_for_owner(game, int(owner), bstate)
        champion = (game.player_champion_card_id
                    if owner_uid_value == game.player_uid
                    else game.ai_champion_card_id)
        if champion and getattr(champion, "uid", None) is not None:
            game.push_player_updated(
                owner_uid_value, champ_id=champion)
    return new_flags
