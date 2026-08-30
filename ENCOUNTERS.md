# HEX TCG — Encounter Reference

Launch encounters from the debug console:
```
camp.encounter <scene_name>     e.g. camp.encounter AZ0_Necrotic
camp.encounter <guid>           e.g. camp.encounter 7b9a5a0d-...
```

Or via chat:
```
!encounter <guid>
```

## AZ0 Training Encounters (Starter Panorama)

| Race | Opponent | Scene Name | Encounter GUID |
|------|----------|------------|---------------|
| Human | Gareth Kay | `AZ0_Human` | `5227b1b0-193a-45e8-a1a3-8215f20ac95b` |
| Elf | Nerissa | `AZ0_Elf` | `e5cd349e-a77d-4175-8cfc-3565395c2eb4` |
| Coyotle | Whispering Breeze | `AZ0_Coyotle` | `064d32cd-9171-4052-82a2-869eb0017b2a` |
| Orc | Moqui | `AZ0_Orc` | `84cdd061-93ec-4126-a2f6-530ac5217c96` |
| Dwarf | Gwendower | `AZ0_Dwarf` | `de641801-55a2-4df8-b298-d2e91c21718d` |
| Shin'Hare | Sora | `AZ0_Shinhare` | `510ed850-5a3e-437b-875e-7834cacd1865` |
| Vennen | Zilth | `AZ0_Vennen` | `f56ba80f-af31-4d75-a4bc-640f899ddd32` |
| Necrotic | Iddi | `AZ0_Necrotic` | `7b9a5a0d-727f-4df8-923f-1e5a5b24c522` |

## Castle Crayburn Dungeon Encounters

| Node | Name | Encounter GUID | Background |
|------|------|---------------|------------|
| WatchTower | Outer Watchtower | `1e65d03d-f3d8-41e9-b3a1-3600e1756378` | Crayburn Castle |
| Drawbridge | The Drawbridge | `cae9b735-ca90-400f-81bf-a0a763fa3dc3` | Crayburn Castle |
| CastleGate | Castle Gatehouse | `f3c0ac5b-ff09-488c-ad63-f11ff15acdcd` | Crayburn Castle |
| InnerBailey | Inner Bailey | `df073679-4fd2-4434-8aff-6c044d759f91` | Crayburn Castle |
| TowerGate | Tower Gatehouse | `2ba61b7b-6864-4582-a634-f9124fb2fdee` | Crayburn Castle |
| PenworthTower | Tower of Penworth | `5f222319-7b4e-4ba4-b0dc-f9678c000d8b` | Crayburn Castle |

## Encounter Rewards — notes (2026-08-02)

**Encounters do NOT specify rewards in gamedata.** `EncounterScene` records have
no reward field. Rewards live in a separate `RewardTemplate` section (only 5
records, all "Krakens Lair"/placeholder) and are delivered via scripted quest
logic, not encounter data.

**Reward delivery is server-push, not client-request.** The client's
`ClientCampaignManager.CacheRewards` is fed ONLY by server-pushed notifications:
`gameendnotify.Applied`, `InputResponse.Applied`, and `CampUpdateNotify.Applied`.
The server must determine the outcome and attach `EscrowAction`s (`GOLD`, `XP`,
`LEVEL`, `CARD`, `BOACARD`, ...) to `Applied.Completed` BEFORE pushing
`gameendnotify`. The client's loot window
(`UICampaignZoneVMBase.HandlePendingReward`) just renders them. So the server
must know the outcome (e.g. princess human vs frog form) at battle end, prior to
the narrative conversation.

**"Kiss of the Princess" / "Victoria's Secret" quest chain (AZ2):**
- "Princess Victoria" (`c0671e20`) is a ChampionTemplate and the AI champion in
  **Peterson Woods** (AZ2 Node 14A/14B):
  - `5deedd32` = Princess Victoria (vs. Ardent), AI deck `d69fabca`
  - `81d153e6` = Princess Victoria (vs. Underworld), AI deck `8ca8ac9d`
  - Both decks use DeckTemplate `2d4037f7` ("AZ2 - 14 - Peterson Woods"),
    which contains **Kiss of the Princess** (`c7bd54c6`) and the other 3
    variants (`7f0eb2dd`, `70c8dda9`, `73244714`) — +2/+2 vs +3/+3 buffs,
    Lifedrain, Prince synergy.
- "Victoria's Secret" quest (`q_uw_victorias_secret`, QuestTemplate `0fc14070`):
  defeat Princess Victoria → return her locket to Ada the Apparitionist
  (Naagaan, Node 27). No reward field in the quest record.

**"Daphne in Distress" (AZ1) — the princess-saving chain:**
- Encounter "The Sea Witch Unfettered" (`b9bc1f22`, AZ1 Node 06) has mod
  **"Daphne in Distress"** — "Save the transmogrified princess from the evil
  Sea Witch" — granting the player **Princess Savior #1** (`d715ce7b`,
  "Summon Princess Daphne") and **Princess Savior #2** (`5134fdf5`,
  "transform each Princess Daphne into Enchanted Frogs").
- **Princess Daphne** (`686ab5d7`, troop, can't attack/block) has a hidden
  one-shot ability: at end of turn while you control it, trigger conversation
  `88e88d7f` = "AZ1 - Node 6 - Hag - Princess Revert" (hag admits the
  transformation spell; you take Daphne).
- The reward for the full chain is likely **Princess Victoria / Kiss of the
  Princess cards**, granted only when the encounter is completed with the
  princess in human form (not as a frog / Enchanted Frog). The human-vs-frog
  outcome split is NOT encoded in encounter/quest data — it's scripted quest
  logic. **TODO: implement server-side outcome tracking + EscrowAction rewards
  in `gameendnotify`.**
