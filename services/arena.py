"""Frost Ring Arena campaign service handlers.

The handlers in this module own client-facing Arena protocol responses.  FRA
run selection rules live in :mod:`gamemodes.arena`, and the reusable SQL
helpers remain in :mod:`db`.
"""

import json

from db import (_db, db_clear_fra_challengers, db_create_fra_challengers,
                db_get_arena_fight_history, db_get_arena_state,
                db_get_active_fra_challenges, db_get_fra_challenge,
                db_get_fra_challengers,
                db_get_fra_public_base_encounter,
                db_champion_template_health,
                db_roll_fra_start_challenge, db_update_arena_state,
                log_req as _log_req)
from encoder import (compress_gzip, encode_datawrapper,
                     encode_get_challengers_response,
                     encode_objfmt_response)


_ZERO_GUID = "00000000-0000-0000-0000-000000000000"
_ARENA_FIGHT_LIST_TYPE = (
    "System.Collections.Generic.List`1#"
    "Reckoning.Campaign.Messages.Arena.ArenaFight"
)


def _send_response(handler, data_type, resp_inner, comp, session_id, reqid,
                   target, instance, conh, service_uid):
    """Wrap and send a normal Campaign Arena response."""
    resp_body = compress_gzip(resp_inner) if comp else resp_inner
    resp_reqid = reqid | 1
    dw_bytes = encode_datawrapper(resp_reqid, data_type, resp_body, comp,
                                  session_id)
    issuer = (f"0.0.0.0.ServiceCampaign.{service_uid}."
              f"ServicePlayer.{handler.client_uid}.{resp_reqid}")
    handler.scnt += 1
    handler.send({
        "issuer": issuer, "target": target, "instance": instance,
        "reqid": resp_reqid, "c": comp, "conh": conh, "sid": handler.sid,
    }, dw_bytes)
    return len(dw_bytes)


def _arena_fight_fields(fight):
    return [
        ("FightID", "ulong", int(fight.get("fight_id", 1))),
        ("FightTier", "int", int(fight.get("fight_tier", 1))),
        ("FightOrder", "int", int(fight.get("fight_order", 1))),
        ("ArenaInstance", "ulong", int(fight.get("arena_instance", 1))),
        ("ChallengerInstance", "ulong",
         int(fight.get("challenger_instance", 1))),
        ("FightResults", "string", str(fight.get("result", "NONE"))),
        ("ChallengeResponse", "string",
         str(fight.get("challenge_response", "NONE") or "NONE")),
        ("RoundChallenge", "struct", ("Game.Shared.ResourceId", [
            ("m_Guid", "guid", str(fight.get("round_challenge", _ZERO_GUID)))
        ])),
    ]


def _arena_challenger_fields(challenger):
    return [
        ("ChallengerID", "ulong", int(challenger.get("id", 1))),
        ("EncounterDeck", "struct", ("Game.Shared.ResourceId", [
            ("m_Guid", "guid", str(challenger.get("deck", _ZERO_GUID)))
        ])),
        ("ChallengerName", "string", challenger.get("name", "")),
        ("IsBoss", "string", challenger.get("boss", "False")),
        ("Equipment", "coll", (
            "System.Collections.Generic.List`1#Game.Shared.ResourceId", 0,
            [])),
    ]


def _mask_unfought_challenger(challenger, next_index):
    """Hide boss/elite status until the challenger has been fought."""
    if not challenger:
        return challenger
    result = dict(challenger)
    challenger_index = int(result.get("id", 1) or 1) - 1
    if challenger_index >= int(next_index):
        result["boss"] = ""
    if challenger_index > int(next_index):
        public_base = db_get_fra_public_base_encounter(result.get("deck", ""))
        if public_base:
            result.update(public_base)
    return result


def _send_challenger_list(handler, target, instance, reqid, comp, session_id,
                          conh, service_uid, log_prefix=""):
    """Send the current roster projection to refresh ArenaClient's cache."""
    user_id = handler.user_profile["id"]
    arena = db_get_arena_state(user_id)
    challengers = db_get_fra_challengers(user_id)
    if not challengers and arena["deck_id"]:
        challengers = db_create_fra_challengers(user_id)
    prefix = f"{log_prefix} " if log_prefix else ""
    _log_req(f">>> {prefix}GetMasterListOfChallengers (dt=10007): "
             f"{len(challengers)} challengers")
    next_index = int(arena.get("challenger_index", 0) or 0)
    public_challengers = []
    for item in challengers[:20]:
        public = _mask_unfought_challenger(item, next_index)
        public_challengers.append({
            "id": public["id"], "deck": public["deck"],
            "name": public["name"], "boss": public["boss"],
        })
    resp_inner = encode_get_challengers_response(True, public_challengers)
    size = _send_response(handler, 10007, resp_inner, comp, session_id,
                          reqid, target, instance, conh, service_uid)
    _log_req(f"    Sent {prefix}GetMasterListOfChallengers response ({size}b)")


def _arena_mc_challenge_fields(challenge):
    """Encode the client-facing Master of Ceremonies challenge object."""
    challenge = challenge or {}
    return [
        ("ChallengeID", "ulong", int(challenge.get("challenge_order", 0) or 0)),
        ("TemplateID", "struct", ("Game.Shared.ResourceId", [
            ("m_Guid", "guid", str(challenge.get("conversation_guid", _ZERO_GUID)))
        ])),
        ("Header", "string", str(challenge.get(
            "objective_heading", "") or challenge.get("challenge_name", ""))),
        ("Body", "string", str(challenge.get(
            "dialogue_text", "") or challenge.get("objective_text", ""))),
    ]


def _arena_info_fields(arena):
    return [
        ("ArenaID", "ulong", 1), ("PlayerID", "ulong", 1),
        ("GameMode", "enum1", ("Game.Shared.Mechanics.ECampaignDifficulty", 0)),
        ("Wins", "int", int(arena.get("wins", 0))),
        ("Loses", "int", int(arena.get("losses", 0))),
        ("DeckId", "ulong", int(arena.get("deck_id", 0))),
        ("FightId", "ulong", int(arena.get("fight_id", 1))),
        ("LastTierLoss", "int", 0),
        ("GoldPacks", "int", int(arena.get("gold_earned", 0))),
        ("CardPacks", "int", 0),
        ("EquipmentPacks", "int", int(arena.get("chests_earned", 0))),
        ("IsBuyout", "bool", False),
        ("Buffs", "coll", (
            "System.Collections.Generic.List`1#Reckoning.Campaign.Messages.Arena.ArenaBuff",
            0, [])),
    ]


def _arena_payload(user_id):
    arena = db_get_arena_state(user_id)
    challengers = db_get_fra_challengers(user_id)
    if not challengers and arena["deck_id"]:
        challengers = db_create_fra_challengers(user_id)
    history = db_get_arena_fight_history(user_id)
    index = int(arena.get("challenger_index", 0) or 0)
    current = challengers[index] if index < len(challengers) else None
    current_fight = history[index] if index < len(history) else {
        "fight_id": index + 1, "fight_tier": index // 5 + 1,
        "fight_order": index + 1, "challenger_instance": index + 1,
        "result": "NONE",
    }
    arena = dict(arena)
    arena["fight_id"] = current_fight["fight_id"]
    return arena, challengers, current, current_fight, history


def _challenge_for_fight(fight):
    guid = str(fight.get("round_challenge", _ZERO_GUID) or _ZERO_GUID)
    if guid == _ZERO_GUID:
        return None
    return db_get_fra_challenge(conversation_guid=guid)


def _fallback_challenger(current_fight):
    return {
        "id": current_fight["challenger_instance"],
        "name": "Angel of Dawn",
        "deck": "bbfd8f29-e549-4eda-9e21-735620d3b5ff",
        "boss": "False",
    }


def _join(handler, target, instance, reqid, comp, session_id, conh,
          service_uid):
    user_id = handler.user_profile["id"]
    arena = db_get_arena_state(user_id)
    deck_id = arena["deck_id"]
    if (not deck_id or not _db.execute(
            "SELECT 1 FROM decks WHERE id=? AND user_id=?", (deck_id, user_id)
    ).fetchone()):
        _log_req(f">>> JoinCampaignArena (dt=10001): no valid deck")
        resp_inner = encode_objfmt_response(
            ["Game.Client.Network.Campaign.JoinCampaignArenaResponse",
             "System.Boolean"], [("Success", "bool", False)])
        _send_response(handler, 10001, resp_inner, comp, session_id, reqid,
                       target, instance, conh, service_uid)
        return

    arena, _challengers, challenger, current_fight, history = _arena_payload(user_id)
    challenge = _challenge_for_fight(current_fight)
    challenger = challenger or {
        "id": current_fight["challenger_instance"],
        "name": "Angel of Dawn",
        "deck": "bbfd8f29-e549-4eda-9e21-735620d3b5ff",
        "boss": "False",
    }
    challenger = _mask_unfought_challenger(
        challenger, int(arena.get("challenger_index", 0) or 0))
    _log_req(f">>> JoinCampaignArena (dt=10001): deck={deck_id}, "
              f"challenger={challenger['name']}")
    resp_inner = encode_objfmt_response(
        ["Game.Client.Network.Campaign.JoinCampaignArenaResponse",
         "System.Boolean", "System.String",
         "Reckoning.Campaign.Messages.Arena.ArenaData",
         "System.Int32", "System.Int32", "System.UInt64",
         "Game.Shared.Mechanics.ECampaignDifficulty", "System.Boolean",
         "System.Collections.Generic.List`1#Reckoning.Campaign.Messages.Arena.ArenaBuff",
         "Reckoning.Campaign.Messages.Arena.ArenaChallenger",
         "Reckoning.Campaign.Messages.Arena.ArenaFight",
         "Reckoning.Campaign.Messages.Arena.ArenaMCChallenge", _ARENA_FIGHT_LIST_TYPE,
         "Game.Shared.ResourceId", "System.Guid",
         "System.Collections.Generic.List`1#Game.Shared.ResourceId",
         "Game.Shared.Network.Campaign.EJoinCampaignArenaError",
         "System.Int32", "System.String"],
        [("ArenaInfo", "struct", (
            "Reckoning.Campaign.Messages.Arena.ArenaData",
            _arena_info_fields(arena))),
         ("ChallengerData", "struct", (
             "Reckoning.Campaign.Messages.Arena.ArenaChallenger",
             _arena_challenger_fields(challenger))),
         ("EncounterData", "struct", (
             "Reckoning.Campaign.Messages.Arena.ArenaFight",
             _arena_fight_fields(current_fight))),
         ("MCChallengeData", "struct", (
             "Reckoning.Campaign.Messages.Arena.ArenaMCChallenge",
             _arena_mc_challenge_fields(challenge))),
         ("FightHistory", "arenafightlist",
          (_ARENA_FIGHT_LIST_TYPE, len(history), history)),
         ("Error", "int", 0), ("ErrorMessage", "string", ""),
         ("Success", "bool", True)])
    size = _send_response(handler, 10001, resp_inner, comp, session_id,
                          reqid, target, instance, conh, service_uid)
    _log_req(f"    Sent JoinCampaignArena response ({size}b)")
    # ArenaClient caches m_AllFighters across runs and otherwise resolves the
    # FightHistory IDs against the previous run's portraits.
    _send_challenger_list(handler, target, instance, 0, comp, session_id,
                          conh, service_uid, log_prefix="refresh")


def _assign_deck(handler, target, instance, reqid, comp, session_id, conh,
                 inner_obj, service_uid):
    user_id = handler.user_profile["id"]
    try:
        deck_id = int(inner_obj.get("DeckID", 0))
    except (TypeError, ValueError, AttributeError):
        deck_id = 0
    db_update_arena_state(
        user_id, deck_id=deck_id, wins=0, losses=0, challenger_index=0,
        fight_history="[]", gold_earned=0, chests_earned=0, sacks_earned=0)
    challengers = db_create_fra_challengers(user_id)
    # A run starts before opponent #1.  There is no Tier 1 skip path in the
    # extracted server, so this is also the explicit full-run boundary.
    challenge = db_roll_fra_start_challenge(user_id)
    history = db_get_arena_fight_history(user_id)
    challenger = challengers[0] if challengers else {
        "id": 1, "name": "Angel of Dawn",
        "deck": "bbfd8f29-e549-4eda-9e21-735620d3b5ff", "boss": "False",
    }
    challenger = _mask_unfought_challenger(challenger, 0)
    _log_req(f">>> AssignArenaDeck (dt=10003): deck={deck_id}, "
              f"challenger={challenger['name']}, "
              f"start_challenge={challenge['challenge_name'] if challenge else 'none'}")
    resp_inner = encode_objfmt_response(
        ["Game.Client.Network.Campaign.AssignArenaDeckResponse",
         "Reckoning.Campaign.Messages.Arena.ArenaData", "System.UInt64",
         "Game.Shared.Mechanics.ECampaignDifficulty", "System.Int32",
         "System.Boolean", "System.String",
         "System.Collections.Generic.List`1#Reckoning.Campaign.Messages.Arena.ArenaBuff",
         "Reckoning.Campaign.Messages.Arena.ArenaChallenger",
         "Game.Shared.ResourceId", "System.Guid",
         "System.Collections.Generic.List`1#Game.Shared.ResourceId",
         "Reckoning.Campaign.Messages.Arena.ArenaFight",
         "System.Collections.Generic.List`1#Reckoning.Campaign.Messages.Arena.ArenaFight",
         _ARENA_FIGHT_LIST_TYPE,
         "Game.Shared.Network.Campaign.EAssignArenaDeckError",
         "System.Int32", "System.String", "System.Boolean"],
        [("ArenaInfo", "struct", (
            "Reckoning.Campaign.Messages.Arena.ArenaData",
            _arena_info_fields({"deck_id": deck_id, "fight_id": 1}))),
         ("ChallengerData", "struct", (
             "Reckoning.Campaign.Messages.Arena.ArenaChallenger",
             _arena_challenger_fields(challenger))),
         ("EncounterData", "struct", (
             "Reckoning.Campaign.Messages.Arena.ArenaFight", _arena_fight_fields({
                 "fight_id": 1, "fight_tier": 1, "fight_order": 1,
                 "arena_instance": 1,
                 "challenger_instance": challenger["id"],
                 "result": "",
                 "round_challenge": history[0].get("round_challenge", _ZERO_GUID),
                 "challenge_response": history[0].get("challenge_response", "NONE"),
             }))),
         ("FightHistory", "arenafightlist",
          (_ARENA_FIGHT_LIST_TYPE, len(history), history)),
         ("Success", "bool", True),
         ("Error", "enum1", (
             "Game.Shared.Network.Campaign.EAssignArenaDeckError", 0)),
         ("ErrorMessage", "string", "")])
    size = _send_response(handler, 10003, resp_inner, comp, session_id,
                          reqid, target, instance, conh, service_uid)
    _log_req(f"    Sent AssignArenaDeck response ({size}b)")
    # Assigning a deck creates a new roster, so refresh the client's cached
    # master list even when it still contains the prior run's 20 IDs.
    _send_challenger_list(handler, target, instance, 0, comp, session_id,
                          conh, service_uid, log_prefix="refresh")


def _cash_out(handler, target, instance, reqid, comp, session_id, conh,
              service_uid):
    user_id = handler.user_profile["id"]
    arena = db_get_arena_state(user_id)
    gold = arena["gold_earned"]
    _log_req(f">>> DoArenaCashOut (dt=10011): gold={gold}, "
              f"chests={arena['chests_earned']}, sacks={arena['sacks_earned']}")
    db_update_arena_state(
        user_id, deck_id=0, wins=0, losses=0, challenger_index=0,
        fight_history="[]",
        gold_earned=0, chests_earned=0, sacks_earned=0)
    db_clear_fra_challengers(user_id)
    resp_inner = encode_objfmt_response(
        ["Game.Client.Network.Campaign.DoArenaCashOutResponse",
         "System.Boolean", "System.Int32", "System.String",
         "System.Collections.Generic.List`1#Reckoning.Campaign.Messages.Arena.ArenaReward",
         "Reckoning.Campaign.Messages.Arena.ArenaReward", "System.UInt64",
         "System.String", "Game.Shared.Network.Campaign.EDoArenaCashOutError",
         "System.Int32"],
        [("Success", "bool", True), ("GoldWin", "int", gold),
         ("AllLoot", "coll", (
             "System.Collections.Generic.List`1#Reckoning.Campaign.Messages.Arena.ArenaReward",
             0, [])), ("Error", "int", 0), ("ErrorMessage", "string", "")])
    size = _send_response(handler, 10011, resp_inner, comp, session_id,
                          reqid, target, instance, conh, service_uid)
    _log_req(f"    Sent DoArenaCashOut response ({size}b)")
    # ArenaClient intentionally keeps m_AllFighters between lobby visits and
    # only requests this list when it is empty.  Send an empty authoritative
    # list after cash-out so the old run's portraits cannot be reused by the
    # next run.
    _send_challenger_list(handler, target, instance, 0, comp, session_id,
                          conh, service_uid, log_prefix="clear")


def _destroy_arena(handler, target, instance, reqid, comp, session_id, conh,
                   service_uid):
    """Complete the client's post-cashout arena cleanup request."""
    user_id = handler.user_profile["id"]
    db_update_arena_state(
        user_id, deck_id=0, wins=0, losses=0, challenger_index=0,
        fight_history="[]", gold_earned=0, chests_earned=0, sacks_earned=0)
    db_clear_fra_challengers(user_id)
    resp_inner = encode_objfmt_response(
        ["Game.Client.Network.Campaign.DestroyArenaDataResponse",
         "System.Boolean",
         "Game.Shared.Network.Campaign.EDestroyArenaDataError",
         "System.Int32", "System.String"],
        [("Success", "bool", True), ("Error", "enum1", (
            "Game.Shared.Network.Campaign.EDestroyArenaDataError", 0)),
         ("ErrorMessage", "string", "")])
    size = _send_response(handler, 10033, resp_inner, comp, session_id,
                          reqid, target, instance, conh, service_uid)
    _log_req(f"    Sent DestroyArenaData response ({size}b)")
    _send_challenger_list(handler, target, instance, 0, comp, session_id,
                          conh, service_uid, log_prefix="clear")


def _fight_history(handler, target, instance, reqid, comp, session_id, conh,
                   service_uid):
    history = db_get_arena_fight_history(handler.user_profile["id"])
    resp_inner = encode_objfmt_response(
        ["Game.Client.Network.Campaign.GetFightListHistoryResponse",
         _ARENA_FIGHT_LIST_TYPE, "Reckoning.Campaign.Messages.Arena.ArenaFight",
         "Game.Shared.ResourceId", "System.Guid", "System.UInt64", "System.Int32",
         "System.String", "Game.Shared.Network.Campaign.EGetFightListHistoryError"],
        [("FightHistory", "arenafightlist",
          (_ARENA_FIGHT_LIST_TYPE, len(history), history)),
         ("Success", "bool", True),
         ("Error", "enum1", (
             "Game.Shared.Network.Campaign.EGetFightListHistoryError", 0)),
         ("ErrorMessage", "string", "")])
    _send_response(handler, 10009, resp_inner, comp, session_id, reqid,
                   target, instance, conh, service_uid)


def _refresh_info(handler, target, instance, reqid, comp, session_id, conh,
                  service_uid):
    arena, _challengers, challenger, fight, history = _arena_payload(
        handler.user_profile["id"])
    challenger = challenger or _fallback_challenger(fight)
    challenger = _mask_unfought_challenger(
        challenger, int(arena.get("challenger_index", 0) or 0))
    challenge = _challenge_for_fight(fight)
    resp_inner = encode_objfmt_response(
        ["Game.Client.Network.Campaign.RefreshArenaInfoResponse", "System.Boolean",
         "Reckoning.Campaign.Messages.Arena.ArenaData",
         "Reckoning.Campaign.Messages.Arena.ArenaFight",
         "Reckoning.Campaign.Messages.Arena.ArenaChallenger", _ARENA_FIGHT_LIST_TYPE,
         "Reckoning.Campaign.Messages.Arena.ArenaMCChallenge",
         "Game.Shared.ResourceId", "System.Guid", "System.UInt64", "System.Int32",
         "System.String", "Game.Shared.Mechanics.ECampaignDifficulty",
         "Game.Shared.Network.Campaign.ERefreshArenaInfoError"],
        [("Success", "bool", True),
         ("ArenaInfo", "struct", (
             "Reckoning.Campaign.Messages.Arena.ArenaData",
             _arena_info_fields(arena))),
         ("EncounterData", "struct", (
             "Reckoning.Campaign.Messages.Arena.ArenaFight", _arena_fight_fields(fight))),
         ("ChallengerData", "struct", (
             "Reckoning.Campaign.Messages.Arena.ArenaChallenger",
             _arena_challenger_fields(challenger))),
         ("MCChallengeData", "struct", (
             "Reckoning.Campaign.Messages.Arena.ArenaMCChallenge",
             _arena_mc_challenge_fields(challenge))),
         ("FightHistory", "arenafightlist",
          (_ARENA_FIGHT_LIST_TYPE, len(history), history)),
         ("Error", "enum1", (
             "Game.Shared.Network.Campaign.ERefreshArenaInfoError", 0)),
         ("ErrorMessage", "string", "")])
    _send_response(handler, 10013, resp_inner, comp, session_id, reqid,
                   target, instance, conh, service_uid)


def _pick_next(handler, target, instance, reqid, comp, session_id, conh,
               service_uid):
    arena, _challengers, challenger, fight, _history = _arena_payload(
        handler.user_profile["id"])
    challenger = challenger or _fallback_challenger(fight)
    challenger = _mask_unfought_challenger(
        challenger, int(arena.get("challenger_index", 0) or 0))
    resp_inner = encode_objfmt_response(
        ["Game.Client.Network.Campaign.PickNextOpponentResponse",
         "Reckoning.Campaign.Messages.Arena.ArenaData",
         "Reckoning.Campaign.Messages.Arena.ArenaFight", "Game.Shared.ResourceId",
         "System.Guid", "Reckoning.Campaign.Messages.Arena.ArenaChallenger",
         "System.Int32", "System.UInt64", "System.String",
         "Game.Shared.Mechanics.ECampaignDifficulty", "System.Boolean",
         "System.Collections.Generic.List`1#Game.Shared.ResourceId",
         "Game.Shared.Network.Campaign.EPickNextOpponentError", "System.String"],
        [("ArenaInfo", "struct", (
            "Reckoning.Campaign.Messages.Arena.ArenaData", _arena_info_fields(arena))),
         ("EncounterData", "struct", (
             "Reckoning.Campaign.Messages.Arena.ArenaFight", _arena_fight_fields(fight))),
         ("ChallengerData", "struct", (
             "Reckoning.Campaign.Messages.Arena.ArenaChallenger",
             _arena_challenger_fields(challenger))),
         ("Error", "enum1", (
             "Game.Shared.Network.Campaign.EPickNextOpponentError", 0)),
         ("ErrorMessage", "string", "")])
    size = _send_response(handler, 10005, resp_inner, comp, session_id,
                          reqid, target, instance, conh, service_uid)
    _log_req(f"    Sent PickNextOpponent response ({size}b)")


def _challenger_list(handler, target, instance, reqid, comp, session_id, conh,
                     service_uid):
    _send_challenger_list(handler, target, instance, reqid, comp, session_id,
                          conh, service_uid)


def _get_mc_challenge(handler, target, instance, reqid, comp, session_id,
                      conh, service_uid):
    fight = _arena_payload(handler.user_profile["id"])[3]
    challenge = _challenge_for_fight(fight)
    resp_inner = encode_objfmt_response(
        ["Game.Client.Network.Campaign.GetArenaMCChallengeResponse",
         "Reckoning.Campaign.Messages.Arena.ArenaMCChallenge", "System.UInt64",
         "Game.Shared.ResourceId", "System.Guid", "System.String",
         "Game.Shared.Network.Campaign.EGetArenaMCChallengeError", "System.Int32"],
        [("MCChallenge", "struct", (
            "Reckoning.Campaign.Messages.Arena.ArenaMCChallenge",
            _arena_mc_challenge_fields(challenge))),
         ("Error", "enum1", (
             "Game.Shared.Network.Campaign.EGetArenaMCChallengeError", 0)),
         ("ErrorMessage", "string", "")])
    _send_response(handler, 10027, resp_inner, comp, session_id, reqid,
                   target, instance, conh, service_uid)


def _get_battle_mods(handler, target, instance, reqid, comp, session_id,
                     conh, service_uid):
    user_id = handler.user_profile["id"]
    arena, _challengers, challenger, fight, history = _arena_payload(user_id)
    challenger_index = int(arena.get("challenger_index", 0) or 0)
    active_challenges = db_get_active_fra_challenges(user_id)
    modifications = []
    for challenge in active_challenges:
        try:
            metadata = json.loads(challenge.get("metadata_json", "{}") or "{}")
        except (TypeError, ValueError):
            metadata = {}
        if metadata.get("opponent_scope") != "TierOne" or challenger_index >= 5:
            continue
        if (challenge.get("challenge_key") == "starting_health_15"
                and history
                and str(history[0].get("challenge_response", "NONE")).upper() != "DECLINE"):
            adjustment = int(metadata.get("health_adjustment", 0) or 0)
            base_health = db_champion_template_health(
                (challenger or {}).get("champion_guid"))
            # EncounterModAddChampionHealth with Absolute=true is the client
            # operation that can lower a champion's starting health safely.
            modifications.append({
                "amount": max(1, base_health + adjustment),
                "absolute": True,
                "is_applied": False,
                "round_to_apply": 0,
                "conversation_id": challenge["conversation_guid"],
                "target_player": 0,  # EModTarget.AIPlayer
            })
    resp_inner = encode_objfmt_response(
        ["Game.Client.Network.Campaign.GetArenaBattleModsResponse",
         "System.Collections.Generic.List`1#Reckoning.Game.EncounterModBase",
         "Reckoning.Game.EncounterModBase",
         "Reckoning.Game.EncounterModAddChampionHealth", "System.Int32",
         "System.Boolean", "Game.Shared.ResourceId", "System.Guid",
         "Game.Shared.Mechanics.EModTarget",
         "Game.Shared.Network.Campaign.EGetArenaBattleModsError"],
        [("Modifications", "encountermodlist", (
            "System.Collections.Generic.List`1#Reckoning.Game.EncounterModBase",
            len(modifications), modifications)),
         ("Error", "enum1", (
             "Game.Shared.Network.Campaign.EGetArenaBattleModsError", 0)),
         ("ErrorMessage", "string", "")])
    _log_req(f">>> GetArenaBattleMods (dt=10029): "
              f"challenges={','.join(c['challenge_name'] for c in active_challenges) or 'none'}, "
              f"mods={len(modifications)}")
    _send_response(handler, 10029, resp_inner, comp, session_id, reqid,
                   target, instance, conh, service_uid)


def _update_mc_challenge(handler, target, instance, reqid, comp, session_id,
                         conh, inner_obj, service_uid):
    """Persist the player's ACCEPT/DECLINE decision for the current fight."""
    user_id = handler.user_profile["id"]
    response = inner_obj.get("EncounterData", {}) if isinstance(inner_obj, dict) else {}
    response = response.get("ChallengeResponse", "") if isinstance(response, dict) else ""
    response = str(response or "").upper()
    if response in {"ACCEPT", "DECLINE"}:
        arena = db_get_arena_state(user_id)
        index = int(arena.get("challenger_index", 0) or 0)
        history = db_get_arena_fight_history(user_id)
        if index < len(history) and _challenge_for_fight(history[index]):
            history[index]["challenge_response"] = response
            db_update_arena_state(user_id, fight_history=json.dumps(history))
    resp_inner = encode_objfmt_response(
        ["Game.Client.Network.Campaign.UpdateMCChallengeResponse",
         "Game.Shared.Network.Campaign.EUpdateMCChallengeError", "System.Int32"],
        [("Error", "enum1", (
            "Game.Shared.Network.Campaign.EUpdateMCChallengeError", 0)),
         ("ErrorMessage", "string", "")])
    _send_response(handler, 10019, resp_inner, comp, session_id, reqid,
                   target, instance, conh, service_uid)


def handle_request(handler, target, instance, reqid, comp, session_id, conh,
                   inner_obj, inner_bytes, SERVICE_MAIL_UID, log_req, **_kw):
    """Handle one of the Campaign Arena ``100xx`` requests."""
    if not handler.user_profile:
        return
    if reqid is None:
        reqid = 0
    if handler.user_profile is None:
        return
    # The live protocol uses the historical SERVICE_MAIL_UID value in the
    # ServiceCampaign issuer path; retain that wire contract during extraction.
    if target is None:
        target = ""
    if instance is None:
        instance = ""
    # data_type is supplied through the dispatch closure below via ``_kw``.
    data_type = int(_kw.get("data_type", 0))
    if data_type == 10001:
        _join(handler, target, instance, reqid, comp, session_id, conh,
              SERVICE_MAIL_UID)
    elif data_type == 10003:
        _assign_deck(handler, target, instance, reqid, comp, session_id, conh,
                     inner_obj, SERVICE_MAIL_UID)
    elif data_type == 10005:
        _pick_next(handler, target, instance, reqid, comp, session_id, conh,
                   SERVICE_MAIL_UID)
    elif data_type == 10007:
        _challenger_list(handler, target, instance, reqid, comp, session_id,
                         conh, SERVICE_MAIL_UID)
    elif data_type == 10009:
        _fight_history(handler, target, instance, reqid, comp, session_id,
                       conh, SERVICE_MAIL_UID)
    elif data_type == 10011:
        _cash_out(handler, target, instance, reqid, comp, session_id, conh,
                  SERVICE_MAIL_UID)
    elif data_type == 10013:
        _refresh_info(handler, target, instance, reqid, comp, session_id, conh,
                      SERVICE_MAIL_UID)
    elif data_type == 10019:
        _update_mc_challenge(handler, target, instance, reqid, comp, session_id,
                             conh, inner_obj, SERVICE_MAIL_UID)
    elif data_type == 10027:
        _get_mc_challenge(handler, target, instance, reqid, comp, session_id,
                          conh, SERVICE_MAIL_UID)
    elif data_type == 10029:
        _get_battle_mods(handler, target, instance, reqid, comp, session_id,
                          conh, SERVICE_MAIL_UID)
    elif data_type == 10033:
        _destroy_arena(handler, target, instance, reqid, comp, session_id,
                       conh, SERVICE_MAIL_UID)
