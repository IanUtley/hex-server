"""Focused regression tests for Frost Ring Arena roster selection."""

import os
import json
import random
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tests.test_db import fresh_database

fresh_database()   # bind this process's database before ``db`` is imported

from gamemodes.arena import (
    FIXED_ELITE_RANKS,
    encounter_family,
    is_boss_encounter,
    select_fra_roster,
)


def encounter(base, *, elite=False, min_rank=6, max_rank=19):
    return {
        "base": base,
        "is_boss": False,
        "is_elite": elite,
        "min_rank": min_rank,
        "max_rank": max_rank,
        "name": base,
    }


def test_fixed_elite_positions_and_known_bosses():
    encounters = [
        *[
            encounter(f"Arena_Starter_{index}", min_rank=1, max_rank=4)
            for index in range(6)
        ],
        encounter("Arena_Eternal_Guardian", min_rank=5, max_rank=5),
    ]
    for base in ("Arena_Phenteo", "Arena_Eurig", "Arena_Princess_Cory"):
        encounters.extend((encounter(base), encounter(base, elite=True)))
    # There must be enough ordinary families both for the fixed elite slots
    # and for the ordinary slots that occur before/after them.
    for index in range(20):
        base = f"Arena_Generic_{index}"
        encounters.extend((encounter(base), encounter(base, elite=True)))
    encounters.extend((
        encounter("Arena_Hogarth", min_rank=20, max_rank=20),
        encounter("Arena_Hogarth", elite=True, min_rank=20, max_rank=20),
    ))

    selected = dict(select_fra_roster(encounters, rng=random.Random(7)))

    assert all(selected[position]["is_elite"] for position in FIXED_ELITE_RANKS)
    assert all(
        not selected[position]["is_elite"]
        for position in range(1, 21)
        if position not in FIXED_ELITE_RANKS and position not in (10, 15, 20)
    )
    assert all(is_boss_encounter(selected[position]) for position in (10, 15, 20))
    assert selected[20]["base"] == "Arena_Hogarth"
    assert all(
        not is_boss_encounter(selected[position])
        for position in FIXED_ELITE_RANKS
    )
    assert len({encounter_family(item) for item in selected.values()}) == 20


def test_empty_round_challenge_is_encoded_as_zero_guid():
    import services.arena as arena_service
    import encoder

    fields = {
        name: value
        for name, _type_name, value in arena_service._arena_fight_fields(
            {"round_challenge": ""})
    }
    guid_fields = {
        name: value
        for name, _type_name, value in fields["RoundChallenge"][1]
    }
    assert guid_fields["m_Guid"] == arena_service._ZERO_GUID
    assert encoder._wire_guid("") == arena_service._ZERO_GUID


def test_challenge_answer_text_accepts_and_decline_is_not_supported():
    """Only the authored answer accepts; there is no decline response."""
    import services.arena as arena_service

    challenge = {
        "answer_text": "Take the challenge",
        "metadata_json": json.dumps({"trigger": "challenge"}),
    }
    assert arena_service._resolve_fra_challenge_response(
        challenge, "Take the challenge") == "ACCEPT"
    assert arena_service._resolve_fra_challenge_response(
        challenge, "ACCEPT") == "ACCEPT"
    assert arena_service._resolve_fra_challenge_response(
        challenge, "DECLINE") == ""
    assert arena_service._resolve_fra_challenge_response(
        challenge, "not the authored answer") == ""


def test_battle_setup_keeps_the_attached_challenges():
    """Battle setup exposes the challenge prompt without an accepted answer."""
    import services.arena as arena_service

    challenge = {
        "conversation_guid": "55555555-5555-5555-5555-555555555555",
        "challenge_key": "test_challenge",
        "metadata_json": json.dumps({"trigger": "challenge"}),
    }
    notification = {
        "conversation_guid": "22222222-2222-2222-2222-222222222222",
        "challenge_key": "test_boss_notification",
        "metadata_json": json.dumps({"trigger": "notification"}),
    }
    fight = {"challenge_response": "NONE"}
    with mock.patch.object(
            arena_service, "_arena_payload",
            return_value=({}, [], {}, fight, [])), \
            mock.patch.object(
                arena_service, "db_get_active_fra_challenges",
                return_value=[challenge, notification]), \
            mock.patch.object(
                arena_service, "_challenge_for_fight",
                return_value=challenge), \
            mock.patch.object(
                arena_service, "_fra_challenge_mod_descriptors",
                return_value=[]):
        setup = arena_service.get_battle_modifications(9111)

    assert setup["challenge"] is challenge
    assert setup["active_challenges"] == [challenge, notification]


def test_challenge_conversation_answer_persists_acceptance():
    """Closing the authored challenge conversation records ACCEPT."""
    import services.arena as arena_service

    guid = "55555555-5555-5555-5555-555555555555"
    history = [{"round_challenge": guid, "challenge_response": "NONE"}]
    challenge = {
        "conversation_guid": guid,
        "answer_text": "Continue",
        "metadata_json": json.dumps({"trigger": "challenge"}),
    }
    with mock.patch.object(
            arena_service, "db_get_arena_state",
            return_value={"challenger_index": 0}), \
            mock.patch.object(
                arena_service, "db_get_arena_fight_history",
                return_value=history), \
            mock.patch.object(
                arena_service, "_challenge_for_fight",
                return_value=challenge), \
            mock.patch.object(
                arena_service, "db_update_arena_state") as update_state:
        accepted = arena_service.record_fra_challenge_conversation_answer(
            9112, guid)

    assert accepted is True
    assert history[0]["challenge_response"] == "ACCEPT"
    update_state.assert_called_once()


def test_mc_accept_persists_for_the_current_fight():
    """The fire-and-forget UpdateMCChallenge records the accept."""
    import services.arena as arena_service

    class Handler:
        user_profile = {"id": 9113}

    guid = "55555555-5555-5555-5555-555555555555"
    history = [{"round_challenge": guid, "challenge_response": "NONE"}]
    challenge = {
        "conversation_guid": guid,
        "answer_text": "Continue",
        "metadata_json": json.dumps({"trigger": "challenge"}),
    }
    with mock.patch.object(
            arena_service, "db_get_arena_state",
            return_value={"challenger_index": 0}), \
            mock.patch.object(
                arena_service, "db_get_arena_fight_history",
                return_value=history), \
            mock.patch.object(
                arena_service, "_challenge_for_fight",
                return_value=challenge), \
            mock.patch.object(
                arena_service, "db_update_arena_state") as update_state:
        arena_service._update_mc_challenge(
            Handler(), "", "", 0, 0, "", 0,
            {"EncounterData": {"ChallengeResponse": "ACCEPT"}}, "")

    assert history[0]["challenge_response"] == "ACCEPT"
    update_state.assert_called_once()


def test_cashout_sends_revealed_roster_before_response():
    """Cash-out reveals the completed run before the summary is built."""
    import services.arena as arena_service

    class Handler:
        user_profile = {"id": 7}

        def push_currency_to_client(self, gold_delta=0, platinum_delta=0):
            events.append(("currency", gold_delta, platinum_delta))

    events = []
    with mock.patch.object(
            arena_service, "db_claim_arena_rewards", return_value={
                "success": True, "gold": 200, "new_gold": 10200,
                "loot": [{"gold": 100}, {"gold": 100}],
                "challengers": [{"id": 1, "deck": "deck-1",
                                 "name": "FRA Test", "boss": "True"}]}), \
            mock.patch.object(
                arena_service, "_send_response",
                side_effect=lambda *args, **kwargs: events.append("response")), \
            mock.patch.object(
                arena_service, "_send_challenger_list",
                side_effect=lambda *args, **kwargs: events.append(
                    ("roster", args, kwargs))):
        arena_service._cash_out(Handler(), "ServiceCampaign", "Shared", 9,
                                1, "session", 0, "mail")

    assert events[0][0] == "roster", events
    assert events[0][2]["reveal_all"] is True, events
    assert events[0][2]["challengers"][0]["boss"] == "True", events
    assert events[1] == ("currency", 200, 0), events
    assert events[2] == "response", events


def test_claim_arena_rewards_pays_100_gold_per_sack():
    """Claiming a run converts tracked gold sacks into account gold."""
    import db
    import pve_db

    user_id = 9110
    conn = db._db
    conn.execute(
        "INSERT OR REPLACE INTO users (id, name, gold, platinum) "
        "VALUES (?, ?, ?, ?)", (user_id, "fra-reward-test", 10000, 0))
    history = [{"result": "NONE"} for _ in range(20)]
    history[0].update({"result": "WIN", "fight_id": 1, "fight_tier": 1})
    history[5].update({"result": "WIN", "fight_id": 6, "fight_tier": 2})
    _seed_arena_reward_state(conn, user_id, 6, 0, history)
    conn.execute(
        "UPDATE arena_state SET gold_earned=?, chests_earned=? WHERE user_id=?",
        (3, 0, user_id))
    conn.commit()
    challengers = [_arena_reward_challenger(index) for index in range(20)]

    with mock.patch.object(pve_db, "db_get_fra_challengers",
                           return_value=challengers):
        result = pve_db.db_claim_arena_rewards(user_id)

    assert result["success"] is True
    assert result["gold"] == 300
    assert len(result["loot"]) == 3
    assert all(reward["gold"] == 100 for reward in result["loot"])
    balance = conn.execute(
        "SELECT gold FROM users WHERE id=?", (user_id,)).fetchone()[0]
    assert balance == 10300
    assert pve_db.db_get_arena_state(user_id)["deck_id"] == 0


def test_destroy_arena_responds_and_clears_roster():
    """The client's post-cashout DestroyArenaData request is acknowledged."""
    import services.arena as arena_service

    class Handler:
        user_profile = {"id": 7}

    events = []
    with mock.patch.object(arena_service, "db_update_arena_state"), \
            mock.patch.object(arena_service, "db_clear_fra_challengers"), \
            mock.patch.object(
                arena_service, "_send_response",
                side_effect=lambda *args, **kwargs: events.append("response")), \
            mock.patch.object(
                arena_service, "_send_challenger_list",
                side_effect=lambda *args, **kwargs: events.append("clear")):
        arena_service._destroy_arena(Handler(), "ServiceCampaign", "Shared",
                                     11, 1, "session", 0, "mail")

    assert events == ["response", "clear"], events


def test_start_encounter_fra_flags_are_transient():
    """Only the ReadyForGameSetup session may advertise PvE flags.

    The client calls UIBattle.OnSessionCreated once for the StartEncounter
    response and again for ReadyForGameSetup; each PvE session appends the
    challenge-event handler. Keeping the transient StartEncounter session
    non-PvE leaves exactly one registration, so one conversation objective
    event cannot add two identical rows to the battle challenge panel.
    """
    import hconnect_server

    values = {"flags": 132, "arena_instance": 11, "arena_owner": 22}
    transient = hconnect_server._fra_arena_session_state_data(values, flags=0)
    live = hconnect_server._fra_arena_session_state_data(values)
    transient_fields = {name: value for name, _type, value in transient[1][1]}
    live_fields = {name: value for name, _type, value in live[1][1]}

    assert transient_fields["SessionFlags"][1] == 0
    assert live_fields["SessionFlags"][1] == 132
    # The arena identity stays on both projections.
    assert transient_fields["ArenaInstance"] == 11
    assert transient_fields["ArenaOwner"] == 22
    assert live_fields["ArenaInstance"] == 11
    assert live_fields["ArenaOwner"] == 22


def _arena_reward_challenger(index, *, boss=False, elite=False):
    return {
        "id": index + 1,
        "name": f"FRA Test {index}",
        "champion_guid": "",
        "deck": f"deck-{index}",
        "boss": "True" if boss else "False",
        "is_elite": elite,
        "ai_deck_personality": "Default",
    }


def _seed_arena_reward_state(conn, user_id, index, losses, history):
    conn.execute(
        "INSERT OR REPLACE INTO arena_state "
        "(user_id, deck_id, wins, losses, challenger_index, fight_history) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, 1, 0, losses, index, json.dumps(history)),
    )
    conn.commit()


def _seed_fra_reward_conversations(conn):
    rows = [
        (
            "11111111-1111-1111-1111-111111111111",
            "health_buff_reward", "Health Buff Reward", "[]",
            json.dumps({
                "trigger": "reward",
                "notification_conversation_guid":
                    "22222222-2222-2222-2222-222222222222",
            }),
        ),
        (
            "22222222-2222-2222-2222-222222222222",
            "health_buff_boss_notification", "Health Buff Boss Notification",
            json.dumps([{
                "type": "EncounterModAddChampionHealth",
                "amount": 5,
                "absolute": False,
                "target_player": "UserPlayer",
                "conversation_id":
                    "22222222-2222-2222-2222-222222222222",
            }]),
            json.dumps({"trigger": "notification"}),
        ),
        (
            "33333333-3333-3333-3333-333333333333",
            "challenge_win_strike_removal", "Challenge Win Strike Removal",
            "[]", json.dumps({"trigger": "reward", "effect": "remove_strike"}),
        ),
        (
            "44444444-4444-4444-4444-444444444444",
            "perfected_tier_strike_removal", "Perfected Tier Strike Removal",
            "[]", json.dumps({"trigger": "reward", "effect": "remove_strike"}),
        ),
        (
            "55555555-5555-5555-5555-555555555555",
            "test_accepted_challenge", "Test Accepted Challenge", "[]",
            json.dumps({"trigger": "challenge"}),
        ),
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO fra_challenges "
        "(conversation_guid, challenge_key, challenge_name, "
        "modifications_json, metadata_json, enabled) VALUES (?, ?, ?, ?, ?, 1)",
        rows,
    )
    conn.commit()


def test_elite_win_pairs_reward_with_that_tiers_boss_modification():
    """A three-life elite win queues its reward and the next tier boss mod."""
    import db
    import pve_db
    import services.arena as arena_service

    user_id = 9101
    conn = db._db
    _seed_fra_reward_conversations(conn)
    history = [{"result": "NONE"} for _ in range(20)]
    history[8].update({
        "active_challenges": ["55555555-5555-5555-5555-555555555555"],
        "round_challenge": "55555555-5555-5555-5555-555555555555",
        "challenge_response": "ACCEPT",
    })
    challengers = [_arena_reward_challenger(index) for index in range(20)]
    challengers[8] = _arena_reward_challenger(8, elite=True)
    challengers[9] = _arena_reward_challenger(9, boss=True)
    _seed_arena_reward_state(conn, user_id, 8, 0, history)

    with mock.patch.object(pve_db, "db_get_fra_challengers",
                           return_value=challengers):
        result = pve_db.db_record_arena_fight(
            user_id, True, return_details=True, rng=random.Random(1),
            session_id="elite-test")

    assert result["reward_conversation_guid"] == \
        "11111111-1111-1111-1111-111111111111"
    saved = pve_db.db_get_arena_fight_history(user_id)
    assert saved[8]["reward_conversation_guid"] == \
        "11111111-1111-1111-1111-111111111111"
    assert "22222222-2222-2222-2222-222222222222" in \
        saved[9]["active_challenges"]

    arena = pve_db.db_get_arena_state(user_id)
    with mock.patch.object(
            arena_service, "_arena_payload",
            return_value=(arena, challengers, challengers[9], saved[9], saved)):
        setup = arena_service.get_battle_modifications(user_id)
    assert setup["modifications"][0]["wire_type"] == \
        "Reckoning.Game.EncounterModAddChampionHealth"
    assert setup["modifications"][0]["amount"] == 5


def test_accepted_challenge_win_removes_one_strike():
    """A challenge win restores one lost life and queues its conversation."""
    import db
    import pve_db

    user_id = 9102
    conn = db._db
    _seed_fra_reward_conversations(conn)
    history = [{"result": "NONE"} for _ in range(20)]
    history[6].update({
        "active_challenges": ["55555555-5555-5555-5555-555555555555"],
        "round_challenge": "55555555-5555-5555-5555-555555555555",
        "challenge_response": "ACCEPT",
    })
    challengers = [_arena_reward_challenger(index) for index in range(20)]
    _seed_arena_reward_state(conn, user_id, 6, 1, history)

    with mock.patch.object(pve_db, "db_get_fra_challengers",
                           return_value=challengers):
        result = pve_db.db_record_arena_fight(
            user_id, True, return_details=True, session_id="challenge-test")

    assert result["reward_conversation_guid"] == \
        "33333333-3333-3333-3333-333333333333"
    assert pve_db.db_get_arena_state(user_id)["losses"] == 0


def test_lossless_tier_win_uses_perfected_tier_strike_removal():
    """A lossless tier restores one prior strike at its final fight."""
    import db
    import pve_db

    user_id = 9103
    conn = db._db
    _seed_fra_reward_conversations(conn)
    history = [{"result": "NONE"} for _ in range(20)]
    for index in range(5, 9):
        history[index]["result"] = "WIN"
    challengers = [_arena_reward_challenger(index) for index in range(20)]
    challengers[9] = _arena_reward_challenger(9, boss=True, elite=True)
    _seed_arena_reward_state(conn, user_id, 9, 1, history)

    with mock.patch.object(pve_db, "db_get_fra_challengers",
                           return_value=challengers):
        result = pve_db.db_record_arena_fight(
            user_id, True, return_details=True, session_id="perfect-tier-test")

    assert result["reward_conversation_guid"] == \
        "44444444-4444-4444-4444-444444444444"
    assert pve_db.db_get_arena_state(user_id)["losses"] == 0


def test_boss_loss_costs_life_and_retries_boss_before_advancing():
    """Boss losses stay on that boss; its eventual win advances the tier."""
    import db
    import pve_db

    user_id = 9105
    conn = db._db
    history = [{"result": "NONE"} for _ in range(20)]
    challengers = [
        _arena_reward_challenger(index, boss=index == 4)
        for index in range(20)
    ]
    _seed_arena_reward_state(conn, user_id, 4, 0, history)

    with mock.patch.object(pve_db, "db_get_fra_challengers",
                           return_value=challengers):
        first_loss = pve_db.db_record_arena_fight(
            user_id, False, return_details=True, session_id="boss-loss-1")
        second_loss = pve_db.db_record_arena_fight(
            user_id, False, return_details=True, session_id="boss-loss-2")
        boss_win = pve_db.db_record_arena_fight(
            user_id, True, return_details=True, session_id="boss-win")

    state = pve_db.db_get_arena_state(user_id)
    saved = pve_db.db_get_arena_fight_history(user_id)
    assert first_loss["recorded"] and second_loss["recorded"]
    assert state["losses"] == 2
    assert state["wins"] == 1
    assert state["challenger_index"] == 5
    assert saved[4]["result"] == "WIN"
    assert saved[4]["boss_loss_count"] == 2
    assert not boss_win["tier_one_perfect_flag_awarded"]

    ordinary_user_id = 9106
    ordinary_history = [{"result": "NONE"} for _ in range(20)]
    _seed_arena_reward_state(conn, ordinary_user_id, 3, 0, ordinary_history)
    with mock.patch.object(pve_db, "db_get_fra_challengers",
                           return_value=challengers):
        pve_db.db_record_arena_fight(
            ordinary_user_id, False, return_details=True,
            session_id="ordinary-loss")
    ordinary_state = pve_db.db_get_arena_state(ordinary_user_id)
    assert ordinary_state["losses"] == 1
    assert ordinary_state["challenger_index"] == 4


def test_boss_loss_consumes_attached_boss_notification_rewards():
    """A failed boss attempt removes earned boss-only challenge modifiers."""
    import db
    import pve_db

    user_id = 9107
    conn = db._db
    _seed_fra_reward_conversations(conn)
    notification_guid = "22222222-2222-2222-2222-222222222222"
    ordinary_guid = "55555555-5555-5555-5555-555555555555"
    history = [{"result": "NONE"} for _ in range(20)]
    history[9].update({
        "active_challenges": [ordinary_guid, notification_guid],
        "round_challenge": ordinary_guid,
        "challenge_response": "NONE",
        "challenge_selection_done": True,
    })
    challengers = [
        _arena_reward_challenger(index, boss=index == 9)
        for index in range(20)
    ]
    _seed_arena_reward_state(conn, user_id, 9, 0, history)

    with mock.patch.object(pve_db, "db_get_fra_challengers",
                           return_value=challengers):
        pve_db.db_record_arena_fight(
            user_id, False, return_details=True, session_id="boss-mod-loss")

    saved = pve_db.db_get_arena_fight_history(user_id)[9]
    assert saved["active_challenges"] == [ordinary_guid]
    assert saved["round_challenge"] == ordinary_guid
    assert notification_guid not in [
        challenge["conversation_guid"]
        for challenge in pve_db.db_get_active_fra_challenges(
            user_id, fight_index=9)
    ]


def test_ordinary_challenges_exclude_bosses_but_apply_to_elites():
    """Bosses get earned notifications, not ordinary elite challenges."""
    import db
    import pve_db

    user_id = 9108
    conn = db._db
    selected = {
        "conversation_guid": "55555555-5555-5555-5555-555555555555",
        "challenge_name": "Test ordinary challenge",
    }
    history = [{"result": "NONE"} for _ in range(20)]
    challengers = [_arena_reward_challenger(index) for index in range(20)]
    challengers[8] = _arena_reward_challenger(8, elite=True)
    challengers[9] = _arena_reward_challenger(9, boss=True, elite=True)
    challengers[14] = _arena_reward_challenger(14, boss=True)
    _seed_arena_reward_state(conn, user_id, 8, 0, history)

    with mock.patch.object(pve_db, "db_get_fra_challengers",
                           return_value=challengers), \
            mock.patch.object(pve_db, "db_select_fra_challenge",
                              return_value=selected) as select_challenge:
        elite_result = pve_db.db_prepare_fra_fight_challenge(
            user_id, fight_index=8)
        elite_boss_result = pve_db.db_prepare_fra_fight_challenge(
            user_id, fight_index=9)
        ordinary_boss_result = pve_db.db_prepare_fra_fight_challenge(
            user_id, fight_index=14)

    assert elite_result == selected
    assert elite_boss_result is None
    assert ordinary_boss_result is None
    assert select_challenge.call_count == 1


def test_debug_game_end_commits_fra_before_game_ended_for_win_and_loss():
    """The debug end command must not race ArenaClient's immediate rejoin."""
    import commands
    import pve_db

    class Handler:
        client_reck_id = 77
        user_profile = {"id": 9104}

    class Session:
        session_id = "fra-order"
        session_name = "arena-session"

    events = []

    def prepare(_handler, _db, _session, won):
        events.append(("prepare", won))
        return {"handled": True, "result": {"recorded": True}}

    def push(_handler, _session, won):
        events.append(("game_ended", won))

    def publish(_handler, _prepared, _mail_uid):
        events.append(("publish", _prepared["handled"]))

    with mock.patch.object(
            commands.game_session, "find_session_by_player",
            return_value=Session()), \
            mock.patch.object(
                commands.campaign, "prepare_fra_battle_gameend",
                side_effect=prepare), \
            mock.patch.object(
                commands, "_push_battle_game_end", side_effect=push), \
            mock.patch.object(
                commands.campaign, "publish_fra_battle_gameend",
                side_effect=publish), \
            mock.patch.object(
                pve_db, "db_latest_campaign_for_user", return_value=None):
        commands._cmd_game_end(Handler(), ["victory"])
        commands._cmd_game_end(Handler(), ["defeat"])

    assert events == [
        ("prepare", True), ("game_ended", True), ("publish", True),
        ("prepare", False), ("game_ended", False), ("publish", True),
    ]


if __name__ == "__main__":
    test_fixed_elite_positions_and_known_bosses()
    test_challenge_answer_text_accepts_and_decline_is_not_supported()
    test_battle_setup_keeps_the_attached_challenges()
    test_challenge_conversation_answer_persists_acceptance()
    test_mc_accept_persists_for_the_current_fight()
    test_cashout_sends_revealed_roster_before_response()
    test_claim_arena_rewards_pays_100_gold_per_sack()
    test_destroy_arena_responds_and_clears_roster()
    test_start_encounter_fra_flags_are_transient()
    test_elite_win_pairs_reward_with_that_tiers_boss_modification()
    test_accepted_challenge_win_removes_one_strike()
    test_lossless_tier_win_uses_perfected_tier_strike_removal()
    test_boss_loss_costs_life_and_retries_boss_before_advancing()
    test_boss_loss_consumes_attached_boss_notification_rewards()
    test_ordinary_challenges_exclude_bosses_but_apply_to_elites()
    test_debug_game_end_commits_fra_before_game_ended_for_win_and_loss()
    print("Arena roster tests passed")
