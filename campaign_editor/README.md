# Campaign editor

A local web tool for the campaign zones: both overworlds (AZ1 Howling Plains,
AZ2 Alachian Sea), ten dungeons and six panoramas, picked from the **Zone**
dropdown. Double-click a 🏰 dungeon or 👥 panorama node (or use *Enter*) to
open it; **←** returns to the overworld node it belongs to. Node icons come
from the client's own `POI_*` node markers (dungeons/panoramas, 🍄 Shroomkin
Haus, ⛺ Gaal camps); the client draws these as 3D props, so the editor uses
glyphs.

Panoramas are rendered with three.js (loaded from cdnjs) from the scene's own
`Main Camera`, fitted to the NPC anchors; drag pans, the wheel zooms, *Fit*
resets. NPC markers sit on the scene's `<Npc>_Anchor` points; their
conversations come from the parent node's tables, and their visibility from
the champion's `PANORAMA` campaign row whose `PanoramaNode` is that node.
Animated FX (fog, mist, particles, heat haze) are not drawn.

Originally this was an AZ1-only tool. It shows the overworld exactly as the
client lays it out, joins it to the authored content and to a champion's live
state, and simulates the declarative campaign rules. Admins can edit authored
campaign data and rules from the page; other accounts use the simulator
without changing those shared definitions.

## Log-in and permissions

The editor asks for a Hex account (the game's own name and password;
Steam-only accounts without a password cannot log in).

- **Admins** (`users.flags` `"admin": "true"`) edit the **shared campaign**:
  the files in `campaign/campaign_data/` and `campaign/campaign_rules/`. They also see every
  players' saved characters in the State list.
- **Everyone else** sees the shared campaign read-only. They can simulate it,
  load only their own characters' saved states, change simulator state, and
  save that state back to their character when the server runs that zone from
  the rules. They cannot edit campaign content.
- **Export / Import** are admin-only and read or replace the shared campaign
  documents as one JSON file.

**Save to champion:** starting the simulator *From live state* in a zone the
server runs from the rules loads that champion's real progress
(`campaign_progress`), not a reconstruction. After editing it (flags,
quests, node progress, or *Move champion here*), **Save to champion** writes
it back. The game sees it when the zone is next loaded or the champion logs
in; save while they are not playing. Players can save only their own
champions; admins can save anyone's.

Campaign content is shared across accounts. Legacy `campaign_user_data` rows
from older versions are ignored and retained; character progress remains in
`campaign_progress`. Sessions live in memory (restarting the editor logs everyone out). The
editor listens on 127.0.0.1 unless started with `--host`; put it behind
HTTPS before exposing it.

## Run

```bash
# once, and after any client asset change (needs UnityPy + Pillow)
python3 campaign/AssetExtraction/extract_campaign_editor_assets.py
# then
python3 campaign_editor/server.py          # http://127.0.0.1:8765/
```

The editor lives at the project root (`campaign_editor/`), and so does the
campaign it edits. `paths.py` resolves the locations:
`campaign/` (override with `HEX_CAMPAIGN_ROOT`), `hex-private/Records`
(override with `HEX_RECORDS`), the database at the project root, and the
extracted art in `campaign_editor/generated/`.

`--db PATH` selects the database (default `hconnect.db`). The server binds to
127.0.0.1 only. Campaign files are written only by admins; saving simulator
progress writes to the logged-in player's own `campaign_progress` rows. It
never writes to `hex-private/Records/`. Deep links:
`/#campaign=<campaigns.id>&node=<NodeId>&view=player`.

With a champion selected, **Player view** draws only what that champion's
client shows: visible nodes, and paths whose endpoints are both visible and
that are not in `PublicState.Data.locked_paths`.

## Where the data comes from

| Shown | Source |
|---|---|
| Zones, node positions, paths, POI markers | client prefabs `campaign/az0N/nodes` and `campaign/az0N/<dungeon>/prefabs/nodes` |
| Dungeon art | `campaign/.../prefabs/background` `*Map` meshes (`DM_*` art), textured per triangle |
| Panorama scenes | `AssetBundles/pve/adventurezone0N/p_*.ab` (meshes, camera, NPC anchors; built-in Quad synthesized) |
| Background | `AZMap` terrain meshes: `_MainTex` diffuse with the `_WaterMask` tinted in; each tile is placed by a least-squares UV→world fit over its own mesh |
| Titles, terrain, descriptions + qualifiers, visit events | `hex-private/Records/SceneData.jsonl` (Howling Plains) |
| Conversation text, answers, prerequisites, `ServerScriptEvent`s | `hex-private/Records/ConversationTemplate.jsonl` |
| Node→conversation, quest conversations, encounter scenes, rewards, quest templates, edges | server catalogue tables (`campaign_node_conversations`, `quest_conversations`, `encounter_scenes`, `quest_templates`, `campaign_node_edges`) |
| Live state | `campaigns.state_json` of a champion's AZ1 `AREA` campaign and their `QUEST` rows |

`campaign/azmap/azmap` is the shared continent; AZ1 occupies only part of it.
`generated/` holds extracted client art and is git-ignored.

The Issues tab cross-checks the three sources (prefab vs SceneData nodes,
prefab paths vs `campaign_node_edges`, catalogue GUIDs vs Records). A scene with no AI champion is a
conversation/lore node, not an error.

## Rules and simulator

The rules the simulator evaluates are built by `campaign/campaign_runtime/rules_loader.py`,
which the server's campaign runtime uses too. A zone whose rules file has
`"runtime": "rules"` is played on the live server exactly as simulated here
(see `docs/CAMPAIGN.md`, "Rules runtime"). Quests are campaign-wide, in
`campaign/campaign_rules/quests.json`. `server_playthrough.py` plays the starter run
through the server's own handlers.

**Simulate all** (Simulate tab) plays a whole overworld (AZ1 or AZ2, with the
dungeons and panoramas it leads to) for the chosen race, always winning, one
step at a time so the map follows it, and tallies the rewards the server would
grant (quest, battle, conversation and Shroomkin Haus rewards). `autoplay.py`
picks each step (`POST /api/sim/auto`, read-only); the page applies it through
`/api/sim/apply` / `/api/sim/load` like the manual buttons. Its choice rules are
in the module docstring; `autoplay.run(db, "AZ1", "Human")` plays the same run
offline.

`campaign/campaign_rules/AZ1.json` is the declarative AZ1 rules file, evaluated by
`campaign/campaign_rules.py` (pure, thread-safe; see its docstring for the condition,
effect and action grammar):

- `start` - the opening node, flags and optional simulator keywords;
- `nodes.<id>.reveal|hidden|blocked|marker` - gates on the derived layout;
- `nodes.<id>.entries` - what the node offers; the first entry whose `when`
  holds wins, so one node can offer different conversations/encounters by
  state. `complete_requires_events` keeps a won encounter retryable until the
  battle reports the event (Tamed: `tamed_troop`);
- `hooks` - effects on events (`conversation_end`, `encounter_victory`,
  `script_event`, `quest_start`, ...);
- `quests.<script>.steps[].done_when` - the event that completes each step.

Map visibility is never stored: it is recomputed from flags, quest progress
and node progress, as the server does when it replays its quest hooks.

The draft was generated by `import_az1_rules.py` from the catalogue tables
plus a hand translation of the `campaign.py` quest-start hooks (each gate
cites its source function). Entries the catalogue cannot disambiguate carry
`"when": false` and a `review` note. The rules files and `quests.json` are
now maintained by hand and in the editor: rerunning an importer
(`import_az1_rules.py`, `import_az2_rules.py`, `import_az1_panoramas.py`,
`import_dungeons.py`, `import_encounter_scenes.py`) keeps every node, gate,
hook, quest step, row and editor field already in the files and only adds
what is new in the Records. Each takes `--dry-run` (or writes to a scratch
`--out-dir` / `--output`) to check its diff first.

In the page, **Simulate** starts a new game for a race, or converts a
champion's live campaign; nodes then offer Visit / Finish conversation /
Win / Win + event / Lose, flags and quest steps can be set directly, and
Undo steps back. Starting from a live campaign lists every node where the
rules-derived layout and the server's live map disagree.

```bash
python3 tests/tests_campaign_rules.py
```

## Tags (conversations and node states)

Both are edited as **tag lists**: every tag must hold. Prefix `!` (or
`not `) to negate. Grammar: `campaign_rules.parse_tag`. For "or" logic, use
**"or" groups** (`+ "or" group` in the dialog): a list of tag lists, holding
when any group holds, e.g. the Masked Army's
`[["questActive(az01_q_fort_romor)", "faction(Underworld)"],
["questActive(az01_q_usurper)", "faction(Ardent)"]]`. Existing rules
written with `any`/`not` are shown in this form (`condition_tags`).

| Tag | Holds when |
| --- | --- |
| `faction(Ardent)` | champion faction |
| `ChampionIsRace(Elf)` | champion race |
| `outcome:success` / `outcome:fail` | the node's last battle result |
| `completedQuest(Script)` / `completedQuest(Script,N)` | quest finished / step N (1-based) done |
| `conversation(Name)` / `conversation(Name,Ending)` | that conversation was finished (with that ending) |
| `flag(X)` `questActive(S)` `questStarted(S)` `visited(Node)` `completed(Node)` | as named |
| `counter(Name)` / `counter(Name,N)` | the zone's counter is at least 1 / N; the exact value appears in the simulator's State table |
| `keyword(Name)` | the champion has that status; a matching positive counter also counts |

`keyword(Stealth)` and `keyword(Burning)` are useful for counter-backed
champion statuses. The editor suggests the status names found in `Records`, and
the value field also accepts other names. In a live campaign, the tag evaluator
reads `PublicState.Data.keywords` (or `champion_keywords`); a positive counter
of the same name also satisfies the tag. The simulator's Champion keywords
section lets you add or remove statuses while authoring a rule.
The server does not currently copy active battle counters into campaign data,
so live checks only see keyword or counter values already persisted there.

* **Conversation tags** (Tags… on a conversation card) are saved to
  `campaign/campaign_data/<template>.json` as the row's `trigger_json.tags` and applied
  to the database at server start. The server (`campaign.py`
  `_select_tagged_rows`) offers a tagged conversation while its tags hold,
  ahead of the name/visit heuristics; a non-matching tagged conversation is
  never offered. A plain `outcome:` tag makes it a battle-result conversation.
* **Node state tags** (Edit beside visible / hidden / blocked / completed /
  retryable / marker) are saved to `campaign/campaign_rules/<zone>.json`; the node is
  in that state when all its tags hold. A zone without a rules file gets one
  built from its map paths. States written with `any`/nesting are shown as
  text; saving tags replaces them. The importer keeps tag-list states.
* **Endings**: clicking a conversation opens its question/answer tree. Each
  answer that ends it is an ending, named by its ServerScriptEvent (the only
  choice the client reports) or `A<answer id>` otherwise. Finishing sets
  `conversation(Name)` and `conversation(Name,Ending)`; the server records
  them in the AREA state's `conversation_flags`. Endings without a script
  event (unless the only ending) can be told apart only in the simulator.

## Race filter

The toolbar **Race** hides what a champion of that race never sees: rows
whose tags need another race/faction (or exclude this one) and, for untagged
rows, an authored name naming another race/faction. Per-race scenes and the
other races' starter panoramas are hidden too, and Castle Crayburn's back
button goes to that race's starter panorama.

## Starter panoramas and Castle Crayburn

The starter-panorama and Castle Crayburn data (generated once from the old in-code tables by `import_crayburn.py`, since removed) is editor data:
`campaign/campaign_data/AZ0.json` (each race's starter-panorama conversations, from
`_AZ0_RACE_CONFIG`), `campaign/campaign_data/crayburn.json` (per-race castle
conversations found by Records name, tagged `ChampionIsRace(X)` and
`outcome:success|fail`) and `campaign/campaign_rules/crayburn.json` (each castle node
is blocked until the previous one is completed). Battles are the race scenes
named `<Race> Tutorial <Node>` (zones.json `scene_regex`). The importer
reads campaign.py with `ast` and checks every row against its seed. The
server still runs Crayburn from campaign.py; the rows are applied at startup
but nothing reads the `AZ0`/`Crayburn Castle` templates yet.

## Ending actions (teleport and encounter)

Each conversation ending (or "any ending", `*`) can carry actions, edited
with **Actions…** in the conversation tree and saved as
`trigger_json.actions` (`{"choice_go": ["teleport(AZ1,Node001)"]}`).
`teleport(Zone[,Node])` sends the player to another editor zone;
`encounter(SceneGuid[,ConversationGuid])` starts that battle at the
conversation's node (its result feeds `outcome:` tags there). The scene picker
shows the node's encounter scenes and scenes already used by that conversation;
the optional opening conversation must belong to the same node. When a rules
campaign launch passes this encounter to battle setup, the opening conversation
is queued as an encounter mod and appears at the start of battle. The
importer writes the starter flow from campaign.py: Crayburn quest start ->
`teleport(crayburn)`, the transition's `choice_go` ->
`teleport(AZ1,Node001)`, and the Tower of Penworth victory ->
`teleport(az0-<race>)`. In the simulator a teleport shows **Follow
teleport**, which opens the zone and carries race, flags and quests. The
server still performs these moves in campaign.py code.

## Starter panoramas as rules zones

Each `az0-<race>` panorama has `campaign/campaign_rules/az0-<race>.json` whose nodes
are its NPCs (no paths), generated from
`campaign/campaign_runtime/az0_races.json`. In a panorama the side panel shows **NPC states** -
what the client reads per NPC (`UIPanoramaZoneViewModel.ProcessStateChange`):
shown (`visible`), `hidden`, `completed` (the client hides a completed NPC),
quest giver `!` (`givequest`), turn-in `?` (`turninquest`), `autostart`
and repeatable (`retryable`). The generated states follow campaign.py: the
intro NPC auto-plays and is completed until the Crayburn report; the
trainer (sparring offer `!`) appears after the intro and is completed after
the training success; the quest NPC (Crayburn quest `!`, report `?`)
appears after that. The AZ0 rows' tags order each
NPC's conversations (intro -> sparring offer -> fail/success -> Crayburn
quest -> report -> transition -> transition repeat), the sparring offer's
`choice_battle_yes` and the fail conversation's `retry_battle` run
`encounter(<training scene>)`, and Crayburn's final victory sets
`crayburn_complete`, which the teleport back carries into the panorama.
NPCs outside the camera are pinned to the screen edge; click to pan there.
NPCs with no Records conversations (e.g. Pyke) are never shown, as on the
server.

## Using the simulator

* Click **▶ Choose** on an ending in a conversation tree, a **▶ ending**
  button on a conversation card, or **▶ Win / ▶ Lose** on a scene card: the
  outcome is applied to the simulation (one is started with the toolbar
  Race if none is running), exactly as if the player had chosen it - its
  flags, actions (teleport / encounter) and node completion.
* The **Simulate** tab's **State** table shows everything the simulation
  holds: champion race, location, keywords, counters, a battle in progress (Win / Lose / ✕),
  flags (✕ removes; add any flag, e.g. a `conversation(Name,Ending)`),
  quests (status, step, ✕) and per node/NPC progress (done / retry /
  failed / last battle, ✕ forgets it). **Undo** steps back.
* Map nodes and panorama NPCs show their state: faded and dashed when not
  shown, grey ✓ when completed (the client hides a completed NPC), ↻
  repeatable, ✗ failed, 🔒 blocked, ! / ? quest give / turn-in, ▸
  auto-start, ⚔️ battle in progress; the current location is ringed.

## Panorama scrolling (parallax and 360-degree wrap)

The editor reproduces the client's `ParallaxController`: the camera stays at
its authored position and the level's three panel groups (Left / Center /
Right, under `PanoMeshRoot`) slide sideways by `dRotateY / 360 * 2 *
PanelBoundsX`, each wrapping by `2 * PanelBoundsX` past `+-PanelBoundsX`.
Layers nearer the camera move faster on screen (parallax); the left and
right panels are copies one wrap apart, so the right edge stitches into the
left. The extractor reads each level's controller (`PanelBoundsX`, start
`RotateY`, panel membership of every mesh and NPC anchor) into
`panorama.json` `parallax`. Drag sideways or press ←/→ to scroll; the wheel
zooms; **Fit** returns to the level's start rotation. NPC markers move with
their panel.

## Zone effects (particles, animation, lightning)

`campaign/AssetExtraction/campaign_effects.py` exports each zone's effects and
`static/effects.js` plays them (toolbar **Effects** toggles them):

* **Particle systems** (panoramas and dungeon maps): emitter shapes
  (sphere/hemisphere/cone/box/circle/edge), rate and bursts, start
  lifetime/speed/size/colour/rotation (constants, curves and random
  ranges as authored), velocity/force/gravity, colour/size/rotation over
  lifetime, texture-sheet animation, billboard / stretched / horizontal /
  vertical rendering, additive / alpha / premultiplied blending and
  prewarm. Not simulated: noise, collision, sub-emitters, trails, lights.
  Dungeon maps draw particles top-down with the texture's alpha (no tint).
* **GoAnim** (the client's GoAnim.cs, ported): tint colour, texture
  offset/scale scrolling, gradient colours/alpha and scale pulses, with
  its interval / duration / loop / delay timing.
* **Animated shaders**: `Hex/Animated Grad` family (texture remapped
  between `_ReBlack`/`_ReWhite`, scrolling, alpha texture), scrolling flow /
  distort textures and the waving-cloth shader. These are reconstructed
  from their parameters (the compiled shaders are not readable), so they
  are close, not exact.
* **Lightning**: a flash every ~`FidgetRate` seconds (the controller's own
  Mecanim clips are compressed and not decoded).
* Heat-haze distortion shaders refract the image behind them and are not
  drawn.

Panorama renderers are drawn in Unity's order: sorting order, then far to
near. Particle shaders on plain meshes (glow cards, god rays) are drawn too,
and additive shaders now composite additively.

## Implicit selectors converted to tags

`convert_conversation_tags.py` replaced the seed's hard-coded conversation
keys and name rules with editable tags (in `campaign/campaign_data/AZ1.json` /
`AZ2.json`), reproducing `_az1_node_conversation`'s order through row
priority:

| was | now |
| --- | --- |
| `visit: first` (or a "Step 1" name) | `!talkedAt(Node,2)` |
| `visit: repeat` | `talkedAt(Node)` |
| `state: fortune` (+ visit) | `flag(gaal_fortune)` (+ `!talkedAt` / `talkedAt`) |
| `outcome: success/fail` | `outcome:success` / `outcome:fail` |
| "Ardent"/"Aria"/"Underworld" in the name | `faction(...)` (only where the node has rows for the other faction) |
| trailing race in the name | `ChampionIsRace(...)` |

`talkedAt(Node[,N])` counts conversations finished at the node (the
server's `conversation_visits`). `flag(X)` reads the area's boolean markers
on the server and the same names in the simulator; `gaal_fortune` is set by
Milosh's `gaal_camp_accept` and cleared by the next battle (AZ1 rules hooks,
as campaign.py `consume_fortune`). A node was converted only when the
server's selectors chose the same conversations before and after for every
tested state (first/repeat visits, fortune, both factions, win/loss); the
rest keep the old rules. Run it against a database copy:
`HEX_DB_PATH=/tmp/copy.db python3 campaign_editor/convert_conversation_tags.py [--write]`.

### Converted selectors (summary)

* **Node conversations** (`campaign_node_conversations.trigger_json.tags`):
  all AZ1/AZ2 nodes except Node048. Where campaign.py fell back to a node's
  first row, that row keeps the fallback role through its tags/priority
  (e.g. `[[!talkedAt(N)], [talkedAt(N)]]` for a fallback that is also the
  repeat conversation). Node048 is left on the old rules: its pre-battle
  selector shows "Player already has fortune" without a fortune.
* **Quest conversations** (`quest_conversations.conditions_json.tags`), all
  103 rows: `start` -> `!questStarted(Q)` (+ `completedQuest(P)` for
  `_AZ1_QUEST_START_PREREQUISITES`), `not_complete` -> `questActive(Q)`,
  `complete` -> `questActive(Q)`, `completedQuest(Q,last)`, plus
  `faction(...)`. The panorama NPC selector and map quest markers read the
  tags (ranking by role and priority, the current-objective rank and the
  start-hook requirement are unchanged). One intended change: the map no
  longer shows a quest-giver "!" for a start whose prerequisite quest is
  unfinished (the panorama would not offer it).
* `convert_conversation_tags.py --quests [--write]` regenerates the quest
  tags with the same before/after check over quest-state combinations.

Still in code: Vale of Oberon's node *state* (completed / repeatable /
terminal, `_az1_masked_army_variant`) and the other quest-start hooks, which
are node-state rules the server does not read from `campaign/campaign_rules/` yet.

## Dungeon content by name

Most dungeon SceneData templates leave their nodes' visit events empty or
wired to placeholders, while the content is named after each node.
`import_dungeons.py` writes `campaign/campaign_data/dungeons.json`: every dungeon
conversation matched to its node by name (`catalog.match_item`: the longest
node title the name contains, or a node letter like "Kraken's Gold F - ..."),
tagged `outcome:success|fail` / `faction(...)` from the name. Battles are
matched the same way from the Records encounter scenes at load time
(`scene_match: items` in zones.json), with their variant (Easy/Medium/Hard,
Sister Midnight's forms) and opponent (EncounterDeck -> champion). It also
reports Records events that name another node (The Tranquil Dream and The
Usurper battles are wired one node off) and nodes with no named content.
Devonshire Keep's letter-suffixed scenes map by node ID (`A` -> `NodeA`,
`H1`/`H2` -> `NodeH`), even though its map item names are numbered. The
Courtyard result conversations are explicitly linked to `NodeA` (Gate):
their Records names do not match a map node and SceneData has no GUID links for
them. Fort Romor's Gate entrance conversation is explicitly linked to
`NodeA`, even though its name says Entrance. Its `Defense of Fort Romor`
conversation names omit the leading "The" from the map item and are matched by
the title without that article. Fort Romor's opponent-named battle scenes use
an opt-in fallback to their authored `UIEventBeginEncounter` map links. This
fallback is scoped to Fort Romor because other dungeons can contain placeholder
event links. The Records contain no `Devonshire Keep B` scene. The server runs
only Castle Crayburn as a dungeon; these rows are editor data.

The Tranquil Dream rules start `answer_score` at 0 for the dungeon simulation.
Its `one_point_answer`, `two_point_answer`, and `three_point_answer` script
events add 1, 2, and 3 respectively. Use `counter(answer_score)` in an editor
tag to require at least one point, or `counter(answer_score,N)` for a
threshold; the State table shows the exact score.
