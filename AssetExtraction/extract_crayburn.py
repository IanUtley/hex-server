"""Extract the per-race Crayburn Castle dungeon map from the extracted records.

Reads ConversationTemplate, DeckTemplate, and ChampionTemplate records and
embeds a ``_CRAYBURN_CASTLE`` dict into campaign.py between the
### BEGIN CRAYBURN CASTLE SEED / ### END CRAYBURN CASTLE SEED markers.

The seed now includes per-race AI deck data (deck GUID + champion GUID per
encounter node) so campaign.py can use the correct opponent for each race.

Run from the repo root:
    python3 AssetExtraction/extract_crayburn.py
"""
import json
import os
import re

RECORDS_DIR = os.environ.get("RECORDS_DIR", "Records")
CAMPAIGN_PY = os.path.join(os.path.dirname(__file__), "..", "campaign.py")

BEGIN_MARKER = "### BEGIN CRAYBURN CASTLE SEED"
END_MARKER = "### END CRAYBURN CASTLE SEED"

RACES = ["Human", "Elf", "Coyotle", "Orc", "Dwarf", "Shin'hare", "Vennen", "Necrotic"]
RACE_OPPOSITE = {
    "Human": "Necrotic", "Necrotic": "Human",
    "Coyotle": "Shin'hare", "Shin'hare": "Coyotle",
    "Elf": "Dwarf", "Dwarf": "Elf",
    "Orc": "Vennen", "Vennen": "Orc",
}
NODES = [
    "The Watchtower", "The Drawbridge", "Castle Gatehouse",
    "Inner Bailey", "Tower Gatehouse", "Tower of Penworth",
]
ENCOUNTER_NODES = ["Castle Gatehouse", "Tower Gatehouse", "Tower of Penworth"]

CASTLE_ENCOUNTERS = [
    "1e65d03d-f3d8-41e9-b3a1-3600e1756378",
    "cae9b735-ca90-400f-81bf-a0a763fa3dc3",
    "f3c0ac5b-ff09-488c-ad63-f11ff15acdcd",
    "df073679-4fd2-4434-8aff-6c044d759f91",
    "2ba61b7b-6864-4582-a634-f9124fb2fdee",
    "5f222319-7b4e-4ba4-b0dc-f9678c000d8b",
]


def _parse_jsonl_record(line: str) -> str:
    """JSONL records are JSON-encoded strings containing escaped JSON.
    json.loads returns the inner string, which is the raw gamedata record.
    """
    try:
        return json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return line


def main():
    # ---- conversations ---------------------------------------------------
    conv = {}
    conv_path = os.path.join(RECORDS_DIR, "ConversationTemplate.jsonl")
    for line in open(conv_path, encoding="utf-8"):
        rec = _parse_jsonl_record(line.strip())
        gid = re.search(r'"m_Id"\s*:\s*\{\s*"m_Guid"\s*:\s*"([0-9a-fA-F-]+)"', rec)
        name = re.search(r'"m_Name"\s*:\s*"([^"]*)"', rec)
        if gid and name:
            conv[gid.group(1).lower()] = name.group(1)

    races = {}
    for gid, name in conv.items():
        parts = name.split(" - ")
        if len(parts) >= 3 and parts[0] == "Crayburn Castle" and parts[1] in RACES:
            race = parts[1]
            node = " - ".join(parts[2:])
            g = races.setdefault(race, {
                "quest_start": None, "quest_end": None, "nodes": {}, "ai_decks": {}})
            if node == "Quest Start":
                g["quest_start"] = gid
            elif node == "Quest End":
                g["quest_end"] = gid
            elif node.endswith("Success"):
                g["nodes"].setdefault(node[: -len(" Success")], {})["success"] = gid
            elif node.endswith("Fail"):
                g["nodes"].setdefault(node[: -len(" Fail")], {})["fail"] = gid
            else:
                g["nodes"].setdefault(node, {})["conv"] = gid

    # ---- champion name + race lookup ------------------------------------
    champ_info: dict[str, tuple[str, str]] = {}
    champ_path = os.path.join(RECORDS_DIR, "ChampionTemplate.jsonl")
    for line in open(champ_path, encoding="utf-8"):
        rec = _parse_jsonl_record(line.strip())
        gm = re.search(r'"m_Id"\s*:\s*\{\s*"m_Guid"\s*:\s*"([0-9a-fA-F-]+)"', rec)
        if not gm:
            continue
        guid = gm.group(1).lower()
        nm = re.search(r'"m_Name"\s*:\s*"([^"]+)"', rec)
        rm = re.search(r'"m_Race"\s*:\s*"([^"]+)"', rec)
        if nm and rm:
            champ_info[guid] = (nm.group(1), rm.group(1))

    # ---- per-race AI decks from DeckTemplate ---------------------------
    # Deck name "Crayburn Castle (<Race>) - <Champion>" tells us the
    # OPPONENT's race (the AI champion's race). The player's race is the
    # opposite (RACE_OPPOSITE).
    opponent_decks: dict[str, list[tuple[str, str]]] = {}
    deck_path = os.path.join(RECORDS_DIR, "DeckTemplate.jsonl")
    for line in open(deck_path, encoding="utf-8"):
        rec = _parse_jsonl_record(line.strip())
        if "Crayburn Castle" not in rec:
            continue
        race_m = re.search(r'Crayburn Castle \(([^)]+)\)', rec)
        if not race_m or race_m.group(1) not in RACES:
            continue
        opponent_race = race_m.group(1)
        dg_m = re.search(r'"m_Id"\s*:\s*\{\s*"m_Guid"\s*:\s*"([0-9a-fA-F-]+)"', rec)
        cg_m = re.search(r'"m_ChampionId"\s*:\s*\{\s*"m_Guid"\s*:\s*"([0-9a-fA-F-]+)"', rec)
        if dg_m and cg_m:
            opponent_decks.setdefault(opponent_race, []).append(
                (dg_m.group(1), cg_m.group(1).lower()))

    # Map opponent-race decks to player-race ai_decks
    for opponent_race, decks in opponent_decks.items():
        player_race = RACE_OPPOSITE.get(opponent_race)
        if not player_race:
            continue
        if player_race not in races:
            races[player_race] = {"quest_start": None, "quest_end": None,
                                  "nodes": {}, "ai_decks": {}}
        g = races[player_race]
        for i, (deck_guid, champ_guid) in enumerate(decks):
            if i < len(ENCOUNTER_NODES):
                node = ENCOUNTER_NODES[i]
                cn, cr = champ_info.get(champ_guid, ("?", "?"))
                g["ai_decks"][node] = {
                    "deck_guid": deck_guid,
                    "champion_guid": champ_guid,
                    "champion_name": cn,
                    "champion_race": cr,
                }

    # ---- emit seed block ------------------------------------------------
    data = {
        "encounters": CASTLE_ENCOUNTERS,
        "races": races,
    }
    block = "_CRAYBURN_CASTLE = " + json.dumps(data, indent=1, sort_keys=True)

    src = open(CAMPAIGN_PY, encoding="utf-8").read()
    begin = src.find(BEGIN_MARKER)
    end = src.find(END_MARKER)
    if begin >= 0 and end >= 0:
        end += len(END_MARKER)
        src = src[:begin] + BEGIN_MARKER + "\n" + block + "\n" + src[end:]
    else:
        src += "\n\n" + BEGIN_MARKER + "\n" + block + "\n" + END_MARKER + "\n"
    open(CAMPAIGN_PY, "w", encoding="utf-8").write(src)

    print(f"Embedded _CRAYBURN_CASTLE for {len(races)} races into {CAMPAIGN_PY}")
    for race in RACES:
        g = races.get(race, {})
        nd = g.get("nodes", {})
        ai = g.get("ai_decks", {})
        champ_names = [ai[n].get("champion_name", "?") for n in ENCOUNTER_NODES if n in ai]
        print(f"  {race}: {len(nd)} conv nodes, opponents={champ_names}")


if __name__ == "__main__":
    main()
