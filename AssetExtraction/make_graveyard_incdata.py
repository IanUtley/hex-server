"""Build a sample dungeon (Graveyard, 3 nodes) as a client-readable .incdata file.

Format (TemplateManager.IncrementalLoad / LoadAbilitiesFromBall): gzip of UTF-8 text.
Sections are separated by "$$$---$$$". Each section is "<TypeName>" then one record
per "$$--$$" chunk. Records use the same TDF JSON as hex-private/Records, with the
"_v"/"_t" headers copied from the Castle Crayburn templates.

Art and encounter are reused from existing client content (no new bundles needed):
node art is Crayburn's AdventureZone01 tile, the Unearthed Grave encounter is the
Crayburn watchtower fight. Node MapNodeIds must exist in the client's node prefab.

Usage: python3 AssetExtraction/make_graveyard_incdata.py [out_path]
"""
import gzip
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "campaign" / "incdata" / "graveyard_sample.incdata"

DUNGEON_ID = "0f6a2c1e-7b3d-4e8a-9c52-1d4f7a6b8e90"
CONVERSATION_ID = "3c9e5b7a-2f1d-4a6e-8b04-7d2c9f1e5a63"
CHAMPION_ID = "b41ca664-789c-4958-8386-8e7b339825c8"   # existing champion portrait (placeholder)
ENCOUNTER_ID = "1e65d03d-f3d8-41e9-b3a1-3600e1756378"  # existing Crayburn watchtower fight (placeholder)

DUNGEON_V = '[{ "DungeonTemplate": 7 },{ "SceneData": 3 },{ "DungeonItemData": 13 },{ "CampaignMapDescription": 1 },{ "UIEventBeginConversation": 1 },{ "UIEvent": 1 },{ "UIEventBeginEncounter": 1 }]'
CONV_V = '[{ "ConversationTemplate": 8 },{ "ConversationQuestion": 4 },{ "ConversationAnswer": 4 }]'

ART = {
    "m_NodeScapePath": "_ArtSource\\\\a0006000-a0006499\\\\a0006170.PNG",
    "m_NodeScapeArtNumber": "a0006170",
    "m_NodeScapeSubBundlePath": "AdventureZone01",
}

# (name, title, map node, description, visit events)
NODES = [
    ("001 - Road to Graveyard", "Road to Graveyard", "Entrance",
     "A cold, mist-soaked road winds toward a graveyard of leaning headstones.", []),
    ("002 - Graveyard Entrance", "Graveyard Entrance", "WatchTower",
     "An iron gate hangs open. Beyond it, crooked headstones lean out of the fog.",
     [("conversation", CONVERSATION_ID)]),
    ("003 - Unearthed Grave", "Unearthed Grave", "PenworthTower",
     "Fresh earth is heaped beside a grave that should not have been opened.",
     [("encounter", ENCOUNTER_ID)]),
]

def event_text(kind, guid):
    if kind == "conversation":
        return ('{\n"_t":"Game.Shared.Challenge.Restriction.UIEventBeginConversation", "m_Qualifiers" : [],\n'
                f'"m_ConversationId" : {{ "m_Guid" : "{guid}" }} }}')
    return ('{\n"_t":"Game.Shared.Challenge.Restriction.UIEventBeginEncounter", "m_Qualifiers" : [],\n'
            f'"m_EncounterId" : {{ "m_Guid" : "{guid}" }} }}')

def node_text(i, name, title, map_node, desc, events):
    ev = ",\n".join(event_text(k, g) for k, g in events)
    return (
        "    {\n"
        f'        "m_ID" : {i},\n'
        f'        "m_Name" : "{name}",\n'
        f'        "m_EventsToFireWhenVisited" : [{ev}],\n'
        '        "m_TotemType" : "None",\n'
        f'        "m_MapNodeId" : "{map_node}",\n'
        '        "m_Descriptions" : [ { "m_Type" : "SingleClick", "m_Description" : "' + desc + '", "m_Qualifiers" : [] } ],\n'
        f'        "m_Title" : "{title}",\n'
        '        "m_TerrainType" : "Dungeon",\n'
        f'        "m_NodeScapePath" : "{ART["m_NodeScapePath"]}",\n'
        f'        "m_NodeScapeArtNumber" : "{ART["m_NodeScapeArtNumber"]}",\n'
        f'        "m_NodeScapeSubBundlePath" : "{ART["m_NodeScapeSubBundlePath"]}" }}'
    )

def dungeon_record():
    nodes = ",\n".join(node_text(i, *n) for i, n in enumerate(NODES))
    return (
        "{\n"
        f'"_v":{DUNGEON_V},\n'
        '"_t":"Reckoning.Game.DungeonTemplate", "m_Id" : { "m_Guid" : "' + DUNGEON_ID + '" },\n'
        '"m_Name" : "Graveyard Sample",\n'
        '"m_BackgroundTexture" : "",\n'
        f'"m_ItemData" : [\n{nodes} ]\n'
        "}"
    )

QUESTIONS = [
    ("Pull yourself together. The iron gate hangs open, and the fog swallows the first headstones.", [0]),
    ("You have faced worse than a few crooked headstones. Haven't you?", [1]),
    ("Then what is holding you back? Something in there is whispering your name.", [2]),
    ("Then go. Walk through the gate and be brave.", [3]),
]
ANSWERS = [
    ("My heart is pounding.", 1),
    ("I have. I am not afraid of the dead.", 2),
    ("Only my own nerves.", 3),
    ("I'm going in.", -1),
]

def dict_block(entries):
    return ",\n".join(f"    {i}     : \n{body}" for i, body in entries)

def conversation_record():
    questions = dict_block([
        (i, "{\n"
            f'        "m_QuestionText" : "{text}",\n'
            f'        "m_AnswerIds" : [ {ans[0]} ],\n'
            '        "m_OwnerNameOverride" : "",\n'
            '        "m_OverrideChampionId" : { "m_Guid" : "00000000-0000-0000-0000-000000000000" },\n'
            '        "m_OverrideOwnerGender" : "None",\n'
            '        "m_Appearance" : "Default" }')
        for i, (text, ans) in enumerate(QUESTIONS)])
    answers = dict_block([
        (i, "{\n"
            f'        "m_AnswerText" : "{text}",\n'
            f'        "m_NextQuestionId" : {nxt},\n'
            '        "m_Prerequisites" : [],\n'
            '        "m_EventsToFireWhenSelected" : [],\n'
            '        "m_OnSelectedEvents" : [],\n'
            '        "m_HightlightType" : "None",\n'
            '        "m_Appearance" : "Default",\n'
            '        "m_IsReentrant" : 0 }')
        for i, (text, nxt) in enumerate(ANSWERS)])
    return (
        "{\n"
        f'"_v":{CONV_V},\n'
        '"_t":"Reckoning.Game.Conversations.ConversationTemplate", "m_Id" : { "m_Guid" : "' + CONVERSATION_ID + '" },\n'
        '"m_DesignerId" : 0,\n'
        '"m_StartingQuestionId" : 0,\n'
        f'"m_Questions" : {{\n{questions}\n}},\n'
        f'"m_Answers" : {{\n{answers}\n}},\n'
        '"m_Name" : "Graveyard Sample - Self-Talk at the Gate",\n'
        '"m_OwnerName" : "Inner Voice",\n'
        f'"m_ChampionId" : {{ "m_Guid" : "{CHAMPION_ID}" }},\n'
        '"m_OwnerGender" : "Male",\n'
        '"m_Appearance" : "Default",\n'
        '"m_BuildTag" : "PreviousRelease"\n'
        "}"
    )

def build():
    sections = [
        "SceneData\n$$--$$\n" + dungeon_record(),
        "ConversationTemplate\n$$--$$\n" + conversation_record(),
    ]
    return "\n$$$---$$$\n".join(sections)

if __name__ == "__main__":
    text = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT, "wb") as f:
        f.write(text.encode("utf-8"))
    print(f"wrote {OUT} ({len(text)} chars uncompressed)")
