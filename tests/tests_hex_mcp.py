"""Checks for the read-only Hex MCP data server (``hex_mcp.py``)."""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.test_db import fresh_database

fresh_database()   # bind this process's database before ``db`` is imported

import hex_mcp


ROOT = Path(__file__).resolve().parents[1]


def test_champion_lookup_by_name_and_guid():
    champion = hex_mcp.hex_champion("Princess Victoria")
    assert champion["found"] is True
    assert champion["guid"] == "c0671e20-f2b8-43ba-97e0-8e4b82944118"
    assert champion["name"] == "Princess Victoria"
    assert champion["race"] == "Human"
    assert len(champion["abilities"]) == 3

    by_guid = hex_mcp.hex_champion(champion["guid"])
    assert by_guid == champion

    ambiguous = hex_mcp.hex_champion("Gareth Kay")
    assert ambiguous["ambiguous"] is True
    assert {row["guid"] for row in ambiguous["candidates"]} == {
        "a0a5ef26-6676-4602-9fa2-e1608120d210",
        "a633844d-bb26-4776-9351-aea16b4c71ba",
    }

    # Hero templates that author abilities come from Records, not the seed
    # tables that carry the PvP and encounter champions.
    boat = hex_mcp.hex_champion("Boat")
    assert boat["name"] == "Boat"
    assert boat["starting_health"] == 40
    assert [ability["costs"]["charge_points"] for ability in boat["abilities"]] \
        == [5, 0]
    # Hero abilities are absent from the seed ability tables, so this proves
    # the Records fallback for both the lookup and the effect chain.
    hero_ability = hex_mcp.hex_ability(boat["abilities"][0]["guid"])
    assert hero_ability["found"] is True
    assert hero_ability["effects"][0]["concrete_type"] == \
        "DrawNCardsAbilityEffectTemplate"


def test_encounter_lookup_joins_ai_champion_deck_and_mods():
    encounter = hex_mcp.hex_encounter("SAVAGE LORD")
    assert encounter["name"] == "AZ 1 - NODE 14 - SAVAGE LORD"
    assert encounter["ai_champion"]["name"] == "Savage Lord"
    assert encounter["ai_champion"]["abilities"][0]["name"].startswith("BasicWild5")
    assert encounter["ai_deck"]["card_count"] == 60
    assert encounter["ai_deck"]["distinct_cards"] == 8
    cards = {card["name"]: card for card in encounter["ai_deck"]["cards"]}
    assert cards["Tyrannosaurus Hex"]["quantity"] == 5
    assert cards["Chomposaur"]["abilities"][0]["name"].startswith("Bdeployb")
    # The authored "Resource Rich" setup card is labelled, not left as a GUID.
    mod_card = encounter["mods"][0]["mods"][0]
    assert mod_card == {"kind": "card",
                        "guid": "1bbcc90a-0046-4d64-911b-b1ea69ee5b1c",
                        "name": "Early Sprouts"}
    # A non-battle scene with no AI opponent is still a valid answer.
    dialog = hex_mcp.hex_encounter("AZ 1 - NODE 56 - NORTH DESERT - DIALOG")
    assert dialog["found"] is True
    assert dialog["ai_champion"] is None
    assert dialog["ai_deck"] is None


def test_card_and_ability_expose_the_effect_chain():
    card = hex_mcp.hex_card("Wakizashi Ambusher")
    assert card["card_type"] == "Troop"
    assert card["subtype"] == "Shin'hare Ranger"
    assert card["thresholds"] == [{"shard": "Blood", "count": 1}]
    assert len(card["abilities"]) == 3

    tunnel = card["abilities"][0]
    assert tunnel["guid"] == "95474d1e-ac9b-6c02-cb95-0305ebec42dc"
    assert tunnel["costs"]["activation"] == 2
    assert tunnel["casting_behavior"] == "BasicAction"
    assert [effect["concrete_type"] for effect in tunnel["effects"]] == [
        "TunnelAbilityEffectTemplate"]
    assert tunnel["effects"][0]["order"] == 0
    assert tunnel["effects"][0]["template"]["m_GameText"] == "Tunnel #SELF#."

    # The same ability resolves by name and by GUID, and reports its user.
    detail = hex_mcp.hex_ability(tunnel["guid"])
    assert {"guid": card["guid"], "name": "Wakizashi Ambusher"} in \
        detail["used_by"]["cards"]
    assert hex_mcp.hex_ability(tunnel["name"])["guid"] == tunnel["guid"]

    charge_power = hex_mcp.hex_ability("WildBasic4ArrowrGain5Health")
    assert charge_power["costs"]["charge_points"] == 4
    assert charge_power["costs"]["is_charge_power"] is True
    assert [row["name"] for row in charge_power["used_by"]["champions"]] == [
        "Running Deer"]

    missing = hex_mcp.hex_ability("no-such-ability-xyz")
    assert missing["found"] is False
    assert "hex_search" in missing["hint"]


def test_search_covers_every_kind():
    results = hex_mcp.hex_search("savage")
    kinds = {row["kind"] for row in results["results"]}
    assert {"champion", "encounter", "card"} <= kinds
    assert any(row["name"] == "Savage Lord" and row["kind"] == "champion"
               for row in results["results"])
    assert hex_mcp.hex_search("savage", kind="encounter")["count"] == 1
    try:
        hex_mcp.hex_search("savage", kind="deck")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown kind must be rejected")


def test_stdio_protocol_serves_tools_end_to_end():
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "hex_champion",
                    "arguments": {"query": "Princess Victoria"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "hex_champion", "arguments": {}}},
    ]
    stdin = "".join(json.dumps(request) + "\n" for request in requests)
    finished = subprocess.run(
        [sys.executable, str(ROOT / "hex_mcp.py")],
        input=stdin, capture_output=True, text=True, cwd=str(ROOT), timeout=120,
    )
    assert finished.returncode == 0, finished.stderr
    answers = [json.loads(line) for line in finished.stdout.splitlines() if line]
    # The notification must not produce a response.
    assert [answer["id"] for answer in answers] == [1, 2, 3, 4]

    assert answers[0]["result"]["protocolVersion"] == "2025-06-18"
    assert answers[0]["result"]["serverInfo"]["name"] == "hex-data"
    tools = [tool["name"] for tool in answers[1]["result"]["tools"]]
    assert tools == ["hex_search", "hex_champion", "hex_encounter",
                     "hex_card", "hex_ability"]
    for tool in answers[1]["result"]["tools"]:
        assert tool["inputSchema"]["required"] == ["query"]

    champion = json.loads(answers[2]["result"]["content"][0]["text"])
    assert champion["name"] == "Princess Victoria"
    assert answers[2]["result"]["isError"] is False

    assert answers[3]["result"]["isError"] is True
    assert "query is required" in answers[3]["result"]["content"][0]["text"]


def main():
    tests = [
        test_champion_lookup_by_name_and_guid,
        test_encounter_lookup_joins_ai_champion_deck_and_mods,
        test_card_and_ability_expose_the_effect_chain,
        test_search_covers_every_kind,
        test_stdio_protocol_serves_tools_end_to_end,
    ]
    for test in tests:
        test()
    print(f"PASS {len(tests)} hex mcp tests")


if __name__ == "__main__":
    main()
