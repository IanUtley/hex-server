# Campaign Architecture

This document describes the campaign model used by the private server and the
client-facing Hex campaign protocol. The authoritative server implementation is
`campaign.py`; the persistent schema is declared in `static.py` and the live
state is stored in the `campaigns` table.

## The four campaign types

`campaigns.campaign_type` is serialized to the client as the `CampType` enum:

| Type | Enum value | Purpose in this server |
|---|---:|---|
| `PANORAMA` | 6 | Race-specific starter area and post-dungeon NPC conversations |
| `QUEST` | 4 | Journal entry and its progressive objectives |
| `AREA` | 2 | AZ1/AZ2 overworld map with connected nodes |
| `DUNGEON` | 1 | A dungeon map such as Crayburn Castle |

The protocol also has `WORLD`, `ACHIEVE`, `STRONGHOLD`, and `TEST` enum values,
but they are not full campaign implementations here.

`campaigns.id` is the server's campaign ID. It is independent of the
`template_name` (for example `AZ1`, `Crayburn Castle`, or `az01_tamed`) and of
the champion ID. A campaign row contains the champion owner, campaign type,
start flag, and a JSON `GameplayState` snapshot.

## Lifecycle

The normal client flow is:

1. `qcur4champ` finds the champion's current campaign. An unfinished dungeon
   is preferred, then an unfinished Crayburn journal quest, then the panorama.
2. `getcampsum` returns the `CampSummary` and the Unity resource paths for the
   selected campaign type.
3. `getcampstate` returns the full `GameplayState`.
4. `startcamp` marks the campaign started. For Crayburn it exposes the first
   conversation node.
5. The client sends `locaction`/`StartLoc` when a map location is selected and
   `sendevent` events such as `visit_path`, `conv_done`, `empty_done`, or
   `start` as the location is resolved.
6. An encounter sends `gamestarted`, creates a game session, and runs through
   the normal battle protocol.
7. The battle result calls the campaign game-end path. The server updates the
   campaign state, applies authored rewards, and sends `gameendnotify` with
   `Applied` updates. The client caches those updates and opens its campaign
   loot window.

The client renders a campaign from three related state collections:

```text
GameplayState
├── LocNodes       map/prefab node IDs (Node001, Node002, ...)
├── VisLocs        Location data and progress flags
├── Encounters     scene GUID catalog used by Location.Encounter
└── PublicState    visited nodes/paths, quest markers, and campaign flags
```

`Location.Node` must match a `LocNodes` node name. `Location.Name` is the
user-facing name sent in `StartLoc`. `Location.Encounter` is a scene GUID,
while `Location.ConversationID` is a conversation-template GUID.

## PANORAMA

The panorama is a race-specific NPC scene, not an overworld map. It is used for
the tutorial chain:

```text
intro NPC conversation
        ↓
trainer conversation → training encounter
        ↓
training victory conversation
        ↓
quest-giver conversation → Crayburn Castle
```

The race configuration in `campaign.py` supplies the NPC names, conversation
GUIDs, trainer champion GUID, training encounter, and race-specific prefab.
Training battle completion sets `TutorialDone`, hides the trainer, and reveals
the quest-giver. Training defeat conversations leave the training encounter
retryable.

After Crayburn is complete, the panorama is also used for the report-success
conversation and the race NPC's AZ1 travel conversation. Completing the travel
choice activates the `AREA` campaign at `Into The Woods`.

## AREA

An area is an overworld map. AZ1 uses the Howling Plains `SceneData` record and
the client assets:

```text
BackgroundPrefab: campaign/azmap/azmap
NodesPrefab:      campaign/az01/nodes
```

The authored `SceneData.m_ItemData` supplies map-node IDs, titles, descriptions,
terrain types, and node artwork. The server converts those records into
`VisLocs` and `LocNodes`. The client NodesPrefab supplies the visual paths; the
server-side `campaign_node_edges` table stores their adjacency for movement
validation and visibility. Visibility and enabled flags control which
locations are selectable; `PublicState.Data.visited_paths` records the path
identifiers reported by the client, while `locked_paths` suppresses an
individual path even when both endpoint nodes are visible. Fog of war is
therefore a client rendering effect driven by the server's visible nodes,
visited paths, and locked paths.

For AZ1, the initial state exposes `Node001` and its adjacent `Node002`.
Completing a node, or returning from its encounter in a retryable state,
reveals its graph neighbours. Arrival at an unfinished encounter does not
draw its outgoing paths. An alternate route may reveal a destination node, but
the path from an unfinished node to an unvisited destination remains locked
with `PublicState.Data.locked_paths`. The opening topology includes the direct `Node003`
→ `Node007` route (Dunnwood to The Road of Oaks), which is not represented by
`SceneData` list order:

```text
Node001  Into The Woods
   ↓
Node002  North Feralroot Woods (faction conversation)
   ↓
Node003  Dunnwood (Wild Cub / taming encounter)
   ↓
Node004  Sporemist Hollow (Shroom Haus card choice)
```

The server resolves AZ1 scene records by matching the authored node number to
the `encounter_scenes.name` (`AZ 1 - NODE ...`). This keeps the node map data,
scene GUID, AI deck, opponent champion, abilities/modifiers, and rewards in
separate data tables.

`campaign_node_conversations` is the corresponding catalog for authored AZ1
and AZ2 conversation templates. It is populated from the `AZ1/AZ2 - Node ...`
names in `ConversationTemplate.jsonl` and stores the normalized node ID,
conversation GUID/name, and a small trigger descriptor (first/repeat,
success/fail where the authored name makes that distinction). It is separate
from `conversation_rewards`: the latter records what a completed conversation
grants, while this table records which conversation can be triggered at a
node.

`tools/campaign_editor/` is a local read-only viewer for AZ1: the client's node
layout over the extracted terrain, joined to SceneData, conversations,
encounter scenes and quests, with a per-champion live-state overlay. See its
README for how to extract the assets and run it.
`campaign_rules/AZ1.json` (evaluated by `campaign_rules.py`) is the
declarative form of the AZ1 node gates, offers, hooks and quest steps that
`campaign.py` currently implements in code; the editor simulates it. The
server does not drive the campaign from it yet; the opt-in
`campaign_shadow.py` (`CAMPAIGN_RULES_SHADOW=1` or `!campaign_rules on|off|diff|reset`)
only compares the engine's AZ1 AREA map state against what legacy sends
(visible/completed/repeatable/blocked) and logs differences.

The checked-in `SceneData` records contain node metadata but do not contain the
map path graph. The `campaign_node_edges` seed is extracted from the client
`Nodes`/`Paths` prefab roots in `Hex_Data/resources.assets`: 80 AZ1 paths and
94 AZ2 paths, stored bidirectionally. Deriving edges from `m_ItemData` order
would create false routes.

### Playing the starter run (AZ0 -> Crayburn -> AZ1)

- **Real game:** `!campaign-restart confirm` deletes every campaign row of the
  profile's newest champion and recreates the AZ0 starter panorama; re-select
  the champion. The server's normal transitions then lead through Crayburn
  into AZ1. Without `confirm` it only explains what it would delete.
- **Editor / engine:** `python3 tools/campaign_editor/playthrough.py [Race]`
  auto-plays the same run through `campaign_rules` (all 8 races reach AZ1).

### Quest-driven map gates

Which AZ1 nodes a quest reveals, hides, blocks or marks is data, not code:
`campaign_rules/AZ1.json` `nodes.<id>.visible|hidden|blocked|marker` (edit them
with the node-state tag editor). `campaign_gates.apply` evaluates them against
the champion's quest rows and conversation flags and writes the `PublicState`
lists `_az1_reveal_neighbors` reads; `_sync_az1_quest_gates` runs it on every
state rebuild and `_grant_quests_for_conversation` runs it the moment a quest
starts. The `az1_*_start` visibility hooks (Find Cave-In / Ambling Mesa / Horwich
Sea, Tamed, Cross the River / Zodiac) are gone, and their names are cleared from
existing databases; a tagged quest start row is offered whenever its editor tags
hold. No start hooks remain (the former `az1_smoldering_dead_start`, which moved
the Smoldering Dead battle onto Brink Ridge, was wrong: that battle stays inside
the Tomb of the Rose Knights dungeon). Brink Ridge's blockade nodes
(Node021/023/077) remain in `_az1_reveal_neighbors`.

Node colours on the client map come from `UIDungeonMapNode`: completed is
green, `impassable` red, repeatable blue, visited yellow, otherwise gray. A
location's `/impassable` field also stops the character token there until the
location is completed. The `impassable` node state in `campaign_rules/AZ1.json`
drives it (`campaign_gates.apply` writes it to the location). It is not the
same as `blocked`: a blocked node is hidden and cannot be reached unless already
visited, while an impassable node is reachable but cannot be passed through.
Brink Ridge is impassable once visited and until `flag(blockade_cleared)`; its
northbound neighbours (Node021/023/077) are blocked over the same period.

### Scene reward conditions

An encounter scene's reward entry (`encounter_scenes.rewards_json`, edited from
the node's scene card -> *Rewards...*, saved to `campaign_data/encounter_scenes.json`) is
gated only by `when`: `flag(X)` tags. After a won battle the server turns the
battle's events (currently `tamed_troop`, from the Tamed IntAttr on an opponent
troop) into flags through the AZ1 rules hooks (`encounter_victory` + `scene` +
`events`) and stores them in the campaign's public data, where `flag(X)` tags
read them. A Tamed scene therefore pays `{"when": ["flag(tamed:150b0ed9)"],
"card_guid": <Dire Toad>}`; the same flag completes the node. A
conversation ending named `Diff_<N>` (Army of Myth's difficulty choice) is also
a battle event for the next won battle, so `flag(battle:<scene8>:Diff_6)` gates
a reward on that level; the importer generates its hook. The old
`end_of_game_condition` / `$condition.template_guid` form is still understood by
the server but the editor no longer writes it.

### Stardust rewards

Any reward object (quest rewards, scene entries, conversation rewards) may carry
`"stardust": [{"rarity": "common|uncommon|rare|legendary|promo", "amount": N,
"probability": P}]`. Each entry is rolled independently when the reward is
granted (`probability` is a percentage, default 100); a hit adds the stardust to
the player's inventory item and stardust ledger and appears in the reward window.
Both reward editors have a Stardust section for these.

### Item art in the reward editors

`python3 AssetExtraction/extract_item_icons.py` extracts thumbnails of equipment,
chests/packs, stardust, gems and mercenaries from the client's
`AssetBundles/general/icons.ab` into `tools/campaign_editor/generated/items/`
(git-ignored; keyed by the Records `m_ArtNumber`, with an `index.json` from item
GUID to art). `python3 AssetExtraction/extract_card_art.py` does the same for
cards from `AssetBundles/cardsets/*.harc` into `generated/cards/`. The reward
dialogs show the picked item's picture beside each chest, equipment, stardust
and card picker, and the quest and scene summaries show a strip of pictures.

### AZ1 quest dependencies (read from the Ardent and Underworld playthroughs)

The start conversations at Ambling Mesa (Ardent) / the Cave-In (Underworld) carry
prerequisite `completedQuest(X)` tags, so an NPC only offers a quest once its
predecessor is finished (the panorama NPC list follows these tags):

| Quest start (giver) | Requires |
|---|---|
| Army of Myth (Myaa), Gnomes Come Home (Vincent) | Tranquil Dream |
| The Usurper (Sagebrush) / Fort Romor (Wyatt) | Army of Myth |
| Smoldering Dead (Myaa) | Army of Myth **and** The Usurper / Fort Romor |
| River of Whispers (`q_go_to_az2`: Sagebrush / Wyatt "starts AZ2") | The Usurper / Fort Romor |
| Devonshire Keep (Myaa) | Smoldering Dead |
| Cross the Zodiac River (Wallace) | Cross the Zila River |
| Blood Beneath (Gurtok / Rizzix) | Tamed *(inferred: not offered at the first visit or after Tranquil Dream, offered at the Tamed turn-in visit)* |

No prerequisite: Sea Hag (Kian / Ennis), Tranquil Dream (completes Find the Ambling
Mesa / Cave-In). Evidence and timestamps: `~/hex-playthroughs/*/QUEST_TIMELINE.md`,
`AMBLING_MESA.md`.

### Encounter scenes are owned by the editor

`campaign_data/encounter_scenes.json` holds the full definition of every
encounter scene (336 of them): name, title, gameboard, deck, champion, mods,
rewards, actions and AI personality. `static.ensure_schema` builds the
`encounter_scenes` table from it at every start (`campaign_data.apply_encounter_scenes`);
the Records seed no longer creates scene rows and the old reward "repair" code
is gone. A blank `ai_deck_personality` means auto (the deck evaluator fills it).
Every editor save for a scene writes this file. Regenerate or refresh it with
`python3 tools/campaign_editor/import_encounter_scenes.py` (`--from-records` to
rebuild from the Records, keeping authored values). Encounter *deck cards* still
come from the Records.

### Encounter scene mods

`encounter_scenes.mods_json` is a list of `{title, description, mods: [{guid}],
target?, round?}`. With no `target` it is a scene-wide modifier shown to the
player (the card GUIDs are the mod cards); with `target` (`AIPlayer` / `Player`)
the cards go into that side's setup at `round` when the battle is created. The
scene card's **Mods...** button edits it (card pickers by name or GUID) and saves
to `campaign_data/encounter_scenes.json`. All 336 Records encounter scenes - including the
dungeons' - are now in `encounter_scenes`, so every scene can carry rewards,
actions, mods and an AI personality.

**Card-selector battles.** A mod entry with `"choice": true` lists cards the
player picks one of before the battle (the client's
`UICampaignBattleWithCardSelectorDetails`). Each card may carry `when` (tags;
offered only while they hold) and `pre` / `victory` / `defeat` flag actions
that run, alongside the scene's own actions, only when it was the card chosen:

```json
{"title": "Fort defences", "choice": true, "mods": [
  {"guid": "<Choose Sniper Tower>", "when": ["flag(romor_sniper_tower)"],
   "victory": ["clear_flag(romor_sniper_tower)"]}]}
```

When an encounter location is offered (`_offer_mod_choice`), the server
evaluates the options. With any on offer the location becomes
`ModChoiceEncounter`, its `modlistId` is `/mod_choice_list` and the offered
GUIDs go in `PublicState.Data.mod_choice_list` (only the client's
`UIDungeonZoneViewModel`, used by dungeons and the AREA map, handles it).
With none on offer it stays a normal `Encounter`. The client sends the pick
as `start`'s `OParms[0]`. The server keeps it in `ActiveEncounterModChoice`
only if it was offered, and battle setup applies just that card
(`campaign_rules.chosen_scene_mods`). The "Choose X" cards are AI-side
hidden setup cards ("each opposing champion summons ..."), so leave
`target` blank. In the editor, tick *Player picks one* in **Mods...**; the
simulator then shows a *Win with X* / *Lose with X* button per offered card.

### Encounter AI personality

`encounter_scenes.ai_deck_personality` (Default, Aggressive, BigThreats,
BuildArmy, Burn, HandAdvantage, Reanimation) drives the AI's card valuation.
At startup `ai_deck_strategy.evaluate_deck_strategy` (the Python deck
evaluator, via `evaluate_fra_deck_personalities.py`) fills every blank scene
from its deck; it never overwrites a scene that already has a value. The scene
card's **AI personality** selector shows the evaluator's pick ("Auto - X", with
its per-strategy scores as a tooltip) and saves an override to
`campaign_data/encounter_scenes.json` (`ai_deck_personality`), applied at server start
after the evaluator, so an authored value always wins. Choosing Auto removes
the override.

### Scene actions

`encounter_scenes.actions_json` (`{"pre": [...], "victory": [...], "defeat": [...]}`,
edited from a scene card -> *Actions...*, saved to `campaign_data/encounter_scenes.json`)
holds `set_flag(X)` / `clear_flag(X)` actions run when the battle starts, is won
or is lost. The flags are stored in the campaign's public data (`flag(X)` tags,
reward `when` entries and node gates read them); the editor rebuilds them live as
`scene:<scene8>:<trigger>` rules hooks so the simulator agrees.

`check_tamed()` (victory only) is the taming check: the server looks for a troop
carrying the persisted `Tamed` marker in the opponent's void
(`void_tamed_troop`) and, if one is there, sets `tamed:<scene8>` (first 8
characters of the scene GUID). Taming scenes gate their card reward on
`when: flag(tamed:<scene8>)`, and the node only completes once that flag is
set; a win without a tame leaves it retryable. The five AZ1 taming scenes
(Wild Cub, Cockatwice Chick, Foul Fae, Diamonds Furever, Old Dirty Bustard)
carry it. In the simulator, "Win (tamed)" stands in for the void check. Army of Myth
sets `battle:0b1d5244:Diff_0` on its first win, which is what unlocks its
level-select conversation.

### Quest completion rewards

The AZ1 quest rewards (Find the Ambling Mesa / Cave-In, Cross the Zila River,
Cross the Zodiac River, Gnomes Come Home) are authored in
`campaign_data/quests.json`; the server grants a quest's reward when the
conversation that finishes the quest completes (`_grant_finished_quest_rewards`).
Encounter-specific cards (Blood Beneath, Army of Myth, Sea Witch, Wormoid Queen,
Tamed troops) are scene reward entries in `campaign_data/encounter_scenes.json`.

A quest's completion reward lives on the quest (`quest_templates.rewards_json`,
edited under the editor's **Quests** tab -> *Rewards...*, saved to
`campaign_data/quests.json`) and is granted once per champion when the quest
completes. A conversation finishes a quest with the ending action
`complete_quest(<script>)` and starts one with `start_quest(<script>)`
(afterwards `questStarted(X)` / `completedQuest(X)` hold). Quest conversations
(`quest_conversations`) default to these by role - *start* runs `start_quest`,
*complete* runs `complete_quest` - and the editor's Actions... saves an
override to `conditions_json.actions`. The generated AZ1 rules no longer carry
`start:` hooks. The Crayburn quest (`az01_q_dwarf_crayburn`) is
completed by each race's *Quest End* conversation; its 150 gold / 500 xp / race
chest now belong to the quest (`chest_guid_by_race`), not to eight conversation
rows. Reward keys: `gold`, `xp`, `chest_guid`, `chest_guid_by_race`,
`card_guid_by_race`, `item_guid_by_race`, `cards`, `items`.

### AREA progress rules

- A conversation node completes on `conv_done`.
- An empty node completes on `empty_done`.
- An encounter node completes only through the battle game-end path.
- A completed non-repeatable encounter cannot be started again.
- A conditional encounter (currently taming) remains visible, enabled, and
  repeatable when the battle is won without satisfying its condition.
- The server must not mark an unfinished encounter complete merely because the
  client attempts to travel away from it.

Node002's conversation grants the `az01_tamed` quest and the faction quest:

| Faction | Quest script |
|---|---|
| Ardent (Human, Elf, Coyotle, Orc) | `az01_ar_find_ambling_mesa` |
| Underworld (Vennen, Necrotic, Shin'hare, Dwarf) | `az01_uw_find_cave_in` |

New quest campaigns are sent with `CampSpawnNotify` (`campspawn`). A normal
`cmpupdate` updates an existing cached campaign but does not create a new quest
entry in the client's journal.

### AZ1 observed quest state machine

The AZ1 route reviewed against both the Ardent and Underworld playthroughs is
represented by the same persisted node/quest state rather than by a separate
client map model. Faction- and race-specific authored conversation variants
are selected from `quest_conversations` for the champion that owns the quest:

| State transition | Server result |
|---|---|
| Node012/Winston starts `Cross the Zila River` | reveal and mark Node013/Node014; keep the west Zila bridge gated |
| Node014 prelude → Savage Lord → success | complete the encounter, advance Step1 (`Razortooth Forest`), then expose the Winston objective |
| Winston objective → Weston objective | select the authored conversation on the existing node and keep the node open until its turn-in completes |
| Node019 blockade conversation | leave Brink Ridge incomplete and hide its northbound destinations |
| `q_smoldering_dead` starts | replace the Brink Ridge explanation with its authored encounter; only a successful result clears the blockade |
| Wallace starts `Cross the Zodiac River` | reveal Shadowgrove and mark it; keep the west Zodiac bridge hidden |
| Corrupt Dryad success → Wallace turn-in | advance the quest and unlock the west Zodiac bridge |
| Warren completion | finish the Cross Zodiac quest and grant its authored card and pack |
| Vale of Oberon with Fort Romor/Usurper | select the authored faction-quest conversation, keep Node00R retryable, and reveal/enable Lena Grotto; the finished-quest branch completes Node00R when closed |

The legacy Cross-Zila objective omitted its encounter GUID in the source
`QuestTemplate`. `static.ensure_schema` repairs it to the Node014 Savage Lord
scene (`ab77df1e-5f13-471b-80e7-b7b4824ca280`) so quest markers, retry rules,
journal advancement, and battle rewards share one identity. Encounter and
conversation rewards remain in their authored `rewards_json` and
`conversation_rewards` records; for example, Gnash Bridges grants the
Ravenous Piranha card, Shadowgrove grants the Howling Plains pack plus its
success-conversation equipment, and the Zila/Zodiac turn-ins grant their pack,
equipment, or card rewards.

## DUNGEON

Crayburn Castle is represented as a `DUNGEON` campaign with the real castle
assets:

```text
BackgroundPrefab: campaign/az01/crayburncastle/prefabs/background
NodesPrefab:      campaign/az01/crayburncastle/prefabs/nodes
```

The castle route is an ordered chain:

```text
Entrance → WatchTower → Drawbridge → CastleGate →
InnerBailey → TowerGate → PenworthTower
```

The map still requires physical movement to the next node. The server does not
automatically move the avatar to a later node after a win. It reveals the next
node and waits for the client's path/travel request.

Each race has its own conversation and encounter mapping. A node can be:

- a conversation, which advances on `conv_done`;
- an encounter, which launches the scene attached to that node; or
- a pass-through node, which is completed immediately.

Encounter victories can queue race-specific success conversations, and losses
can queue race-specific defeat conversations. Completing the final castle
objective advances the linked Crayburn quest and exposes the report-success NPC
in the panorama.

## QUEST

`QuestTemplate.jsonl` contains 33 physical lines, but the first is a section
delimiter: there are 32 actual quest-template records in the current extract.
Quest templates contain the quest ID, script name, title/description, and an
ordered list of objectives. They do **not** contain gold, XP, card, equipment,
or loot fields.

The client source defines these objective types:

| Objective type | Metadata reference |
|---|---|
| `QuestObjectiveEncounter` | `m_EncounterId` |
| `QuestObjectiveDungeon` | `m_DungeonId` |
| `QuestObjectiveConversation` | conversation IDs and optional `m_ConversationEvent` |
| `QuestObjectiveCollect` | count and public script variable |

Every objective also has a `m_QuestLocationId`. This is a quest-state location
identifier, not necessarily an overworld map node. The server stores the active
objectives as `QUEST` campaign `VisLocs` with matching `Location.Name` values.

The current implementation reveals only the first objective initially. The
complete ordered objective list is retained in `GameplayState.Flags`, and
`_advance_quest_campaign` marks the current `VisLoc` complete and appends the
next one. This prevents future journal objectives from appearing before they
are reachable.

Quest grant and completion are runtime events rather than fields on
`QuestTemplate`:

- grant: a campaign script/event creates a `QUEST` row and sends `campspawn`;
- encounter objective: matched scene wins advance the objective;
- dungeon objective: dungeon completion advances the objective;
- conversation objective: `conv_done` completes the active objective;
- collection objective: progress is based on the relevant persisted variable.

Quest-start map changes are editor data: node `visible` / `hidden` /
`blocked` / `marker` tags in `campaign_rules/AZ1.json` that read
`questStarted(X)` etc. (see above). `quest_templates.start_hook` can still name
a whitelisted content hook in `campaign.py` (`_QUEST_START_HOOKS`), but none is
registered or seeded.

The extracted `m_ConversationEvent` values are empty in the current data, so the
server currently uses explicit campaign events and GUID mappings.

## Linkage between conversations, scenes, encounters, and quests

The practical relationship is:

```text
ConversationTemplate GUID
        │
        └── Location.ConversationID
                │  client closes conversation
                └── sendevent conv_done

SceneData node ──► Location.Node/Name
                      │
                      └── Location.Encounter (scene GUID)
                              │
                              └── encounter_scenes
                                  ├── AI deck/champion
                                  ├── mods_json
                                  └── rewards_json

QuestTemplate script
        └── QuestObjective*.m_QuestLocationId
                │
                └── QUEST GameplayState.VisLocs
```

Starting an encounter persists `ActiveEncounterGuid` on the campaign state.
The battle setup uses that GUID to resolve the opponent and deck; the game-end
reward path uses the same GUID to resolve `rewards_json`. This avoids falling
back to the race trainer scene when an AZ1/AZ2 node is being played.

## Encounter rewards

Rewards are authored on `encounter_scenes.rewards_json`, not on quest templates.
The schema is intentionally JSON so a scene can have one compact record or a
list under `end_of_game_rewards`/`end_of_game`:

```json
{"gold": 100, "xp": 100}
```

```json
{
  "end_of_game_condition": {"type": "void_tamed_troop", "owner": "opponent"},
  "card_guid": "$condition.template_guid",
  "quantity": 1,
  "one_time": true,
  "gold": 100,
  "xp": 100
}
```

Supported authored fields in the current server are:

| Field | Meaning |
|---|---|
| `gold` | Add to `users.gold` and emit a `GOLD/GRANT` reward |
| `xp` | Add to `champions.xp`, recalculate level, and emit `XP/GRANT` |
| `card_guid` or `template` | Add card copies to the collection |
| `quantity` | Number of card copies; defaults to one |
| `one_time` | Claim once per campaign scene; defaults to true |
| `end_of_game_condition` | Gate a conditional reward |
| `end_of_game_rewards`/`end_of_game` | List of reward records |
| `card_choice` | Card choices for a Shroom Haus location |

The current condition evaluator supports `void_tamed_troop`. It looks in the
finished game session's Void for an opponent-owned troop with the persisted
`permanent_buffs.int_attrs.Tamed` marker. `$condition.template_guid` resolves
to that captured card's template GUID. A failed conditional taming encounter
remains retryable; the node is not treated as permanently complete.

`card_choice` is handled separately from battle end. The client sends
`sendevent` with `Event=shroom_choice`; the server validates the selected GUID
against the scene's authored choices, adds the card to the collection, emits
the card reward, and completes the Shroom Haus location. The server does not
choose one of the three cards automatically.

### Reward delivery to the client

For a campaign battle win, `handle_battle_gameend` applies the scene rewards
before sending `gameendnotify`. The payload uses the client-compatible
`AppliedUpdates` shape:

- `Accounts` contains the updated balance;
- `Champions` contains updated XP/level;
- `Cards` contains newly granted card instances;
- `Completed` contains `ItemAction=GRANT` entries that trigger the client's
  pending campaign loot window.

The client recognizes additional `Completed.ItemKind` values such as `LEVEL`,
`CARD`, `BOACARD`, `BOAITEM`, `ITEM`, `MERCPAC`, and `MERCXP`. The current
`rewards_json` evaluator directly authors only gold, XP, card GUIDs, and Shroom
Haus card choices; other reward kinds require corresponding server-side
`AppliedUpdates` handling.

Rewards are not emitted for a loss. One-time claims are persisted in the
campaign state's `_encounter_reward_claims`, so reconnecting or retrying a
completed scene does not duplicate them.

## Champion XP thresholds

The current campaign reward path treats XP as cumulative champion XP. The
server recalculates the level using these thresholds:

| Level | Total XP required |
|---:|---:|
| 1 | 0 |
| 2 | 1,000 |
| 3 | 2,800 |
| 4 | 5,000 |
| 5 | 8,000 |
| 6 | 12,000 |
| 7 | 17,500 |
| 8 | 25,000 |
| 9 | 40,000 |
| 10 | 50,000 |
| 11 | 60,000 |
| 12 | 70,000 |
| 13 | 80,000 |
| 14 | 92,000 |
| 15 | 110,000 |

The current implementation caps this calculation at level 15. A 100 XP
encounter reward therefore updates the champion's cumulative XP but normally
does not level a new champion immediately.

## Scene and reward data maintenance

`encounter_scenes` is created in `static.py`. Existing databases receive
`ai_champion_guid`, `mods_json`, and `rewards_json` through the schema-repair
path in the same module. Encounter and deck rows are seeded from the extracted
gamedata; authored AZ0/tutorial/AZ1 reward defaults are then merged without
replacing scene-specific card choices or conditional rewards.

Useful inspection queries:

```sql
SELECT guid, name, ai_deck_guid, ai_champion_guid, mods_json, rewards_json
FROM encounter_scenes
WHERE name LIKE 'AZ 1%';

SELECT id, champion_id, template_name, campaign_type, state_json
FROM campaigns
WHERE champion_id = ?
ORDER BY id;
```

When changing campaign code, validate with:

```bash
python3 -m py_compile campaign.py
python3 tests/verify_goldens.py
```

For client-visible failures, inspect
`/mnt/d/SteamLibrary/steamapps/common/HEX SHARDS OF FATE/Hex_Data/output_log.txt`
first, then `/tmp/hconnect_log.txt` and `/tmp/hconnect_requests.log`. Changes
to `campaign.py` require `bash restart.sh`; reload-only modules can use the
server's `SIGUSR1` hot-reload path.
