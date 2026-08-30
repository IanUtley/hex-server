# HEX TCG — Card Attributes (`ECardAttributes`)

The `ECardAttributes` bitmask is stored in `card_templates.attributes` (static
template flags) plus `game_cards.card_attributes` (instance-granted flags). The
effective attributes are the OR of both. Several flags are combined masks
(noted below).

| Value | HEX enum | Client ToolName | MTG counterpart | Notes |
|------:|----------|----------------|-----------------|-------|
| 1 | `SpiritDrain` | Life Drain | Lifelink | Controller gains life equal to damage this card deals. |
| 2 | `Flight` | Flight | Flying | Can only be blocked by troops with Flight or Sky Guard. |
| 4 | `Speed` | Speed | Haste | May attack the turn it enters play (ignores summoning sickness). |
| 8 | `SkyGuard` | Sky Guard | Reach | Can block Flight attackers. |
| 16 | `Juggernaught` | Crush | Trample | Assigns enough damage to kill blockers, leftover damage breaks through to the champion. |
| 32 | `Steadfast` | Steadfast | Vigilance | Does not tap when attacking. |
| 64 | `Immortal` | Invincible | Indestructible | Does not die to damage or card effects; still dies to state-based effects (e.g. 0 defense) and sacrifice. |
| 128 | `SpellShield` | Spell Shield | Hexproof (targeted) | Can't be targeted by opponent's spells/abilities. |
| 256 | `Unique` | Unique | Legendary | Only one copy may be in play per player. |
| 512 | `CantAttack` | Can't Attack | Can't attack | |
| 1024 | `CantBlock` | Can't Block | Can't block | |
| 2048 | `Defensive` | Defensive | Defender | May not attack. |
| 4096 | `ForceAttack` | Must Attack | Attacks each combat if able | |
| 8192 | `CantReadyAutomatically` | No Auto-Ready | Doesn't untap during untap step | |
| 16384 | `FirstStrike` | Swift Strike | First strike | Deals combat damage in the first-strike damage step. |
| 32768 | `Rage` | Rage | — (HEX) | +ATK when this card is damaged. |
| 65536 | `MustBlock` | Must Block | Blocks each combat if able | |
| 131072 | `CantBeBlocked` | Can't be Blocked | Can't be blocked | |
| 262144 | `PreventCombatDamage` | Prevent Combat Damage | Prevents all combat damage dealt to it | |
| 524288 | `PreventNonCombatDamage` | Prevent Non-Combat Damage | Prevents all non-combat damage | |
| 786432 | `PreventAllDamage` | Prevent All Damage | Prevents all damage | `PreventCombatDamage \| PreventNonCombatDamage` |
| 1048576 | `DualStrike` | Dual Strike | Double strike | |
| 2097152 | `CantInflictCombatDamage` | Can't Inflict Combat Damage | Deals no combat damage | |
| 4194304 | `CantInflictNonCombatDamage` | Can't Inflict Non-Combat Damage | Deals no non-combat damage | |
| 6291456 | `CantInflictAnyDamage` | Can't Inflict Any Damage | Deals no damage | `CantInflictCombatDamage \| CantInflictNonCombatDamage` |
| 8388608 | `EntersPlayExhausted` | Enters play Exhausted | Enters the battlefield tapped | |
| 16777216 | `Inspire` | Inspire | — (HEX) | When this enters play, each other ally you control gets +1/+1 until end of turn. |
| 33554432 | `Escalation` | Escalation | — (HEX) | Additional costs grow each time the card is played. |
| 67108864 | `DoesntReadyNextReadyStep` | Doesn't ready next ready step | Doesn't untap next untap step | |
| 134217728 | `VoidsDamagedTroops` | Troops dealt damage are voided | — (HEX) | Troops it damages are voided (exiled) instead of dying. |
| 268435456 | `QuickAction` | Can be played as a Quick Action | Flash | May be played at instant speed. |
| 536870912 | `AllowYardInspire` | Allows Inspire from Graveyard | — (HEX) | Inspire also triggers from the graveyard. |
| 1073741824 | `MustBeBlocked` | Must be blocked | — (HEX) | Opponent must block this card if able. |
| −2147483648 | `Boon` | Boon | — (HEX) | Boon card-type modifier. |

## Combat-relevant rules (server behaviour)

- **Flight / Sky Guard** — a troop may only block an attacker with Flight if the
  blocker itself has Flight or Sky Guard.
- **Speed** — a troop with Speed may attack the turn it enters play (it is
  exempt from summoning sickness: `StartedATurnOnYourSide` is not required).
- **Immortal** — survives lethal combat/effect damage and destroy effects, but
  still dies to state-based death (defense reduced to 0 or less by modifiers)
  and to sacrifice costs.
- **Juggernaught (Crush / Trample)** — the attacker assigns enough of its attack
  to kill each blocker (in damage-assignment order), and any remaining damage
  breaks through to the defending champion.
- **SpiritDrain (Lifelink)** — controllers gain life equal to the damage their
  cards deal in combat.
