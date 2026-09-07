# AZ1 campaign playthrough notes

These notes are the working state ledger for the two supplied Adventure Zone 1
playthroughs:

- Ardent / Elf Ranger: `dbY98jo9NR0`
- Underworld / Mortuus: `KY0Sgw9A2yQ` split playlist, with the long
  `NtjwmoktSfk` recording used as a secondary reference

The videos have no usable captions, so text is recorded only where it is
readable in the video or corroborated by the authored records/database. Battle
turns are not campaign state; the important boundaries are map selection,
conversation completion, encounter launch, victory/defeat, reward display, and
the next map state.

## State to preserve for every map node

The map needs these independent values; `completed` must not be inferred from
being visited or from returning to the map after a battle.

| State | Meaning |
|---|---|
| `visible` | The node is revealed through fog of war or a quest hook. |
| `enabled` | The node can currently be selected/travelled to. |
| `completed` | Its conversation, empty action, or encounter has actually finished. |
| `repeatable` | A completed node may be entered again. |
| `type` | Empty, conversation, Shroom Haus/choice, or encounter. |
| `conversationId` | The conversation selected for this node and current quest state. |
| `pre_encounter` / `pre_encounter_completed` | Optional conversation before battle. |
| `encounter` | The exact encounter-scene GUID, not only the display title. |
| `outcome_conversation` | Success/failure conversation still waiting after battle. |
| `visible/enabled` gates | Quest, blockade, bridge, faction, and failed-encounter gates. |
| `visited_nodes` / `visited_paths` | Persistent movement history reported by the client. |

The campaign-level ledger also needs:

`LastNode`, `ALoc`, `quest_nodes`, `quest_reveal_nodes`, `quest_hidden_nodes`,
`blocked_nodes`, `blockade_cleared`, each quest's `_quest_objective_idx`,
`ActiveEncounterGuid`, `_pending_encounter_success`, `_encounter_successes`,
`_encounter_reward_claims`, `_conversation_reward_claims`, and the last
encounter condition/defeat state. A reward screen being shown is not proof that
the associated conversation or quest objective has been advanced.

## Video-observed progression checkpoints

### Shared opening

Both campaigns show the same broad AZ1 progression: the initial route reaches
the welcome/faction conversation, then the Wild Cub/taming route, Sporemist
Hollow, the Oaks/forest branch, and the river/bridge branch. The map legend
distinguishes new, incomplete, completed, blocked, repeatable, and quest nodes;
these are separate client-visible states and should be retained separately in
the server state.

### Ardent route

The Ardent run shows the following campaign boundaries:

1. Into the Woods -> North Feralroot Woods: the opening faction conversation
   grants the taming and Ardent faction progression.
2. Dunnwood/Wild Cub: the taming encounter remains a progression objective,
   not merely a one-time completed node.
3. Sporemist Hollow: the Shroom Haus choice is a conversation/choice action,
   not a normal encounter victory.
4. Vale of Oberon: Masked Sergeant/quest dialogue exposes the Zila route.
5. Zila River/Gnash Bridges: this is the source of the Ravenous Piranha
   reward; the card reward is tied to the authored encounter/reward record.
6. Razortooth Forest/Savage Lord: the victory is followed by a conversation,
   then the map returns with Recover a specimen from Razortooth Forest as the
   active quest state.
7. Bridge over the Zila - West/Weston: the bridge node remains a conversation
   turn-in boundary; travelling to it must not silently complete the quest.
8. Brink Ridge: the first visit presents the blockade explanation. The node
   remains incomplete and its northbound destinations remain hidden until the
   Smoldering Dead route clears the blockade.
9. Shadowgrove/Corrupt Dryad: success produces the authored reward flow and
   advances the Cross the Zodiac River state; it does not by itself complete
   the Wallace turn-in.
10. The later run reaches Army of Myth in Forsaken Plateau. Its pre-battle
    Talana/Crow Feather conversation is visible before the battle.

### Underworld route

The Underworld split run confirms the same state boundaries while using the
Underworld faction/champion and its own fog-of-war presentation:

- The Savage Lord episode opens on the map with the node selected, then shows
  the Savage Lord conversation and the Comet Strike encounter setup. After
  victory, a Savage Lord outcome conversation is shown before the map returns.
  The returned map has Razortooth Forest selected and the specimen objective
  visible in the quest panel.
- The following Corrupt Dryad episode starts at Razortooth Forest, travels via
  Bridge over the Zila - West, shows the authored pack/equipment reward flow,
  and later returns to a map with Shadowgrove selected after the Dryad battle.
  This confirms that reward display, inventory update, map return, and quest
  advancement are separate state transitions.
- The Army of Myth episode shows the same encounter identity but a different
  faction champion/map presentation. It is entered through a map/conversation
  boundary and ends with encounter rewards; the Ardent recording additionally
  makes the post-battle Army resource choice readable.

## Army of Myth rules to model

The encounter must track an explicit encounter parameter/choice:

1. The map node is selected and its pre-battle conversation completes.
2. The encounter opens with the Army of Myth scene and faction-specific
   champion/opponent presentation.
3. After victory, the conversation asks how much of an advantage to give the
   Army of Myth. The visible choices are 1, 2, 3, 4, 5, or 6 starting resources.
4. The selected value is an encounter outcome variable. It must not be
   confused with the node's `completed` flag or with the player's normal
   resource pool.
5. Only after the outcome conversation and reward acknowledgement should the
   node/quest state advance.

## Full AZ1 map inventory

This is the complete 85-node `SceneData` inventory. It is the map coverage
ledger; each row still requires the state fields above and the authored
conversation/encounter metadata to be joined at runtime.

| Node | Authored title | Node | Authored title |
|---|---|---|---|
| Node001 | Into The Woods | Node002 | North Feralroot Woods |
| Node003 | Dunnwood | Node004 | Sporemist Hollow |
| Node005 | Fonferek Thicket | Node006 | Horwich Sea |
| Node007 | The Road of Oaks | Node008 | The Bleak Citadel |
| Node009 | The Sutherland | Node00R | Vale of Oberon |
| Node00X | Short Cut | Node00Y | River of Whispers |
| Node00Z | River of Whispers | Node010 | Sylvan Peninsula |
| Node011 | Fort Romor | Node012 | Bridge over the Zila - East |
| Node013 | Zila River | Node014 | Razortooth Forest |
| Node015 | Bridge over the Zila - West | Node016 | The Road of Grass |
| Node017 | Cave-In | Node018 | Shadowgrove |
| Node019 | Brink Ridge | Node019B | Brink Ridge |
| 019B1 | Brink Ridge | 019B2 | Brink Ridge |
| 019B3 | Brink Ridge | Node019C | Brink Ridge |
| Node020 | Dreamsmoke Prairie | Node021 | Clark Foothills |
| Node022 | Lake Wyalusing | Node023A | Saramago Crater |
| Node023B | Saramago Crater | Node024 | Tomb of the Rose Knights |
| Node025 | Lena Grotto | Node026A | Johannes Bog |
| Node026B | Johannes Bog | Node027 | Silver Forest |
| Node028 | Garden of Ophelia | Node029 | Hoff Mines |
| Node030 | Bridge over the Zodiac - East | Node031 | Zodiac River |
| Node032 | Zodiac River - North | Node033 | Indigo River |
| Node034 | Ambling Mesa | Node035 | Uuug's Oasis |
| Node037 | Blood Gate | Node038 | Bridge over the Zodiac - West |
| Node039 | Moonrise Canyon | Node040 | Indigo River Bend |
| Node041 | Sunsoul Basin | Node042 | Bauer Dunes |
| Node044 | Wildwood | Node045 | The Burk Heart |
| Node046 | Barking Forest | Node047 | Indigo Plains |
| Node048 | The Road of Sand | Node049 | The Thunderfield |
| Node050 | Crescent Mesa | Node051 | Startouched Valley |
| Node052 | Badlands | Node053 | South Desert |
| Node054 | Southern Stretch | Node055 | Deep Desert |
| Node056 | Northern Stretch | Node057 | North Desert |
| Node058 | Northeast Desert | Node059 | East Desert |
| Node060 | Southeast Desert | Node061 | Northwest Desert |
| Node062 | West Desert | Node063 | Southwest Desert |
| Node064 | The Scar | Node065 | Sky'le Tepui |
| Node066 | Forsaken Plateau | Node067 | Devonshire Hills |
| Node068 | Devonshire Keep | Node069 | Painted Woods |
| Node070 | Steepdale | Node071 | Valley of Sighs |
| Node073 | Sapphire Gate | Node074 | Dusk Hollow |
| Node075 | Eagle Mesa | Node077 | Blau Flats |
| Node078 | Skittering Ridge |  |  |

The graph itself is separate from this inventory. Movement/reveal state must be
driven by `campaign_node_edges`/the extracted Nodes prefab paths, not by the
order of this table. In particular, the authored path-fork nodes and the
Zila/Zodiac bridge edges need their own visible/enabled gates.

## Quest and gate ledger

| Trigger | Reveal/advance | Must remain gated until |
|---|---|---|
| Welcome conversation | `az01_tamed` and faction quest | Conversation completion, not arrival alone |
| Taming objectives | Tame1-Tame5, then Belarius | The relevant encounter condition is met |
| Winston at Vale of Oberon | Cross the Zila River Step1; reveal Node013/014 | West Zila bridge remains gated |
| Savage Lord success | Recover specimen objective advances | Outcome conversation/reward flow completes |
| Weston at Zila West | Advance Cross Zila turn-in | Conversation completion |
| Brink Ridge blockade | Keep node incomplete; hide northbound routes | Successful Smoldering Dead result |
| Wallace at Zodiac East | Start Cross the Zodiac River; reveal/mark Shadowgrove | West Zodiac bridge remains hidden |
| Corrupt Dryad success | Advance to Wallace turn-in | Wallace conversation completion |
| Warren completion | Finish Cross the Zodiac and grant authored rewards | All preceding objective states |

## Remaining review questions

- Confirm the exact Army of Myth choice/result packet and whether the selected
  resource count is persisted in campaign state or only in the encounter setup.
- Verify the Underworld recording's post-Army outcome conversation, since the
  available episode ends around the reward return and the choice is clearest in
  the Ardent recording.
- Continue annotating optional branches (Shroom Haus, Blacksmith, desert,
  Devonshire, and faction-specific blockade variants) with the same state
  fields rather than treating them as ordinary completed nodes.
