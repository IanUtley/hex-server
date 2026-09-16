"""RulesPort-owned visibility projection for hidden-information effects."""

from __future__ import annotations

import json
import game_engine

from gamedata import DEFAULT_RECORD_STORE, ability_graph
from .conditions import ConditionContext, evaluate_effect_condition
from .metadata import modifier_metadata
from .runtime_helpers import owner_uid


_HAND_VISIBILITY_ATTR = "canseeopponentshand"


def _json_list(value):
    try:
        value = json.loads(value or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _champions(handler):
    callback = getattr(handler, "_champion_targets", None)
    try:
        return callback() or [] if callable(callback) else []
    except Exception:
        return []


def _active_modifier(db, session, handler, pl_t, ai_t, state, card_uid,
                     owner_id, ability_guid):
    graph = ability_graph(DEFAULT_RECORD_STORE, str(ability_guid).lower())
    if graph is None:
        return False
    for effect in graph.effects:
        meta = modifier_metadata(effect.guid)
        if (effect.concrete_type != "CardModifierAbilityEffectTemplate" or
                meta.get("property") != "intattr" or
                str(meta.get("attribute") or "").lower() !=
                _HAND_VISIBILITY_ATTR):
            continue
        index = int(effect.target_index)
        if index < 0 or index >= len(graph.targets) or \
                graph.targets[index].target_kind != "PlayerTargetTemplate":
            continue
        condition_id = str(effect.condition_guid or "")
        if condition_id and condition_id != "0" * 36:
            context = ConditionContext(
                db, session, state, ability_source_uid=int(card_uid),
                ability_source_owner_id=int(owner_id),
                ability_source_card_owner=int(owner_id),
                trigger_owner_id=int(owner_id), pl_t=pl_t, ai_t=ai_t,
                champions=_champions(handler))
            if not evaluate_effect_condition(db, condition_id, context):
                continue
        try:
            if int(meta.get("value", meta.get("input_value", 1)) or 0) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        return True
    return False


def _opponent_owner(state, owner_id, handler):
    if state.get("pvp"):
        pids = [int(pid) for pid in state.get("pids", ())]
        if not pids:
            pids = [int(pid) for pid in (state.get("champ_map") or {})]
        return next((pid for pid in pids if pid != int(owner_id)), None)
    if int(owner_id) == 0:
        profile = getattr(handler, "user_profile", None) or {}
        return int(profile.get("id", 0)) if profile else None
    return 0


def _owner_uid(game, owner_id, state):
    return owner_uid(int(owner_id), game.player_uid, game.ai_uid, state)


def _push_hand(db, session, handler, game, pl_t, ai_t, state, viewer, visible):
    opponent = _opponent_owner(state, viewer, handler)
    if opponent is None:
        return
    from pvp_db import db_visibility_hand_rows
    viewer_uid = _owner_uid(game, viewer, state)
    for uid, template, card_owner, card_type, card_state in db_visibility_hand_rows(
            session.session_id, int(opponent), conn=db):
        scid = game_engine.SessionCardId(game_engine.UID(int(uid)))
        try:
            _tpl, ctype, _name, cost, attack, defense, gems = \
                handler._card_full_data(game, scid, template)
        except Exception:
            continue
        game.push_card_updated(
            scid, _owner_uid(game, card_owner, state),
            game_engine.ECardCollections.Hand, ctype,
            template_id=template, state=int(card_state or 0), cost=cost,
            attack=attack, defense=defense, gems=gems, nulling=not visible)
        game.events[-1]._hand_reveal_viewer_uid = viewer_uid


def project_visible_hands(db, session, handler, game, pl_t, ai_t, state):
    flags = state.get("player_visibility") or {}
    projected = getattr(game, "_visible_hand_uids", None)
    if projected is None:
        projected = {}
        game._visible_hand_uids = projected
    from pvp_db import db_hand_card_uids
    active = set()
    for owner, attrs in flags.items():
        owner = int(owner)
        if not int((attrs or {}).get("CanSeeOpponentsHand", 0) or 0):
            continue
        opponent = _opponent_owner(state, owner, handler)
        if opponent is None:
            continue
        current = {int(row[0]) for row in db_hand_card_uids(
            session.session_id, opponent, conn=db)}
        key = str(owner)
        previous = projected.get(key)
        if previous is None or current - set(previous):
            _push_hand(db, session, handler, game, pl_t, ai_t, state, owner, True)
        projected[key] = sorted(current)
        active.add(key)
    for key in list(projected):
        if key not in active:
            projected.pop(key, None)
    return projected


def _apply_player_flags(game, state):
    game._visibility_by_uid = {
        int(getattr(_owner_uid(game, int(owner), state), "uid64",
                    _owner_uid(game, int(owner), state))): dict(attrs or {})
        for owner, attrs in (state.get("player_visibility") or {}).items()
    }


def apply_player_visibility_to_game(game, state):
    """Apply persisted viewer permissions to a fresh network projection."""
    projected = {}
    for owner, attrs in (state or {}).get("player_visibility", {}).items():
        try:
            uid = _owner_uid(game, int(owner), state or {})
            projected[int(getattr(uid, "uid64", uid))] = dict(attrs or {})
        except (TypeError, ValueError):
            continue
    game._visibility_by_uid = projected


def refresh_player_visibility(db, session, handler, game, pl_t, ai_t, state):
    from pvp_db import db_visibility_underground_rows
    old = state.get("player_visibility") or {}
    new = {}
    for uid, owner, abilities in db_visibility_underground_rows(
            session.session_id, conn=db):
        for guid in _json_list(abilities):
            if _active_modifier(db, session, handler, pl_t, ai_t, state,
                                uid, owner, guid):
                new.setdefault(str(int(owner)), {})["CanSeeOpponentsHand"] = 1
    changed = {str(owner) for owner in set(old) | set(new) if bool(
        (old.get(owner) or {}).get("CanSeeOpponentsHand")) != bool(
        (new.get(owner) or {}).get("CanSeeOpponentsHand"))}
    state["player_visibility"] = new
    _apply_player_flags(game, state)
    if state.get("pvp"):
        champ_map = state.get("champ_map") or {}
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
    for owner in changed:
        visible = bool((new.get(owner) or {}).get("CanSeeOpponentsHand"))
        _push_hand(db, session, handler, game, pl_t, ai_t, state,
                   int(owner), visible)
    project_visible_hands(db, session, handler, game, pl_t, ai_t, state)
    for owner in set(old) | set(new):
        owner_uid_value = _owner_uid(game, int(owner), state)
        champion = (game.player_champion_card_id
                    if owner_uid_value == game.player_uid
                    else game.ai_champion_card_id)
        if champion and getattr(champion, "uid", None) is not None:
            game.push_player_updated(owner_uid_value, champ_id=champion)
    return new
