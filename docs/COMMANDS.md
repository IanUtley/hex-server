# Chat Command Reference

All commands use `!` prefix. Type in chat (any tab works after session room join).
The server accepts these commands only when `allowcon` is present in
`HEX_PROFILE_FLAGS`.

## Card Info & Manipulation

| Command | Usage | Description |
|---------|-------|-------------|
| `!hand` | `!hand` | List cards in hand: `CardName [card_id]` |
| `!zones` | `!zones` | List all cards grouped by zone (one message per zone) |
| `!playable` | `!playable [id|name ...]` | Set which cards get golden outlines. No args = all playable. |
| `!drawcard` | `!drawcard <id\|name> [id\|name ...]` | Draw specific cards from deck to hand |
| `!draw` | `!draw N` | Draw N cards from deck |
| `!top` | `!top <id\|name>` | Move a card from your hand to the top of your deck |
| `!discard` | `!discard` | Prompt to choose a card from hand to discard (DiscardACard target picker) |
| `!move` | `!move <card_id> <zone>` | Move card: `deck`, `hand`, `warzone`, `discard`, `void`, `playedresources`, `underground` |
| `!update` | `!update <card_id>` | Resend CardUpdated event for a card (restores thresholds/gems) |

## Card State & Attributes

| Command | Usage | Description |
|---------|-------|-------------|
| `!state` | `!state <id> <flags>` | Set card state: `Tapped\|Attacking\|Blocking\|Damaged\|Healed\|Dead\|HasAttacked\|HasBlocked\|EffectExpired\|Activated` |
| `!attr` | `!attr <id> <flags>` | Set attributes: `Flight\|Speed\|SkyGuard\|Crush\|Steadfast\|Invincible\|SpellShield\|Unique\|LifeDrain` |

Flags can be pipe-separated (`Tapped\|Attacking`) or space-separated. Unknown flags are rejected without changes.

## Resources & Champion

| Command | Usage | Description |
|---------|-------|-------------|
| `!threshold` | `!threshold [me\|opp] C B R S W D` | Set 6 threshold counts (Colorless Blood Ruby Sapphire Wild Diamond) |
| `!resource` | `!resource [me\|opp] <current> <maximum>` | Set current and maximum resources |
| `!charge` | `!charge [me\|opp] <N>` | Set champion charges |
| `!spellpoints` | `!spellpoints [me\|opp] <N>` | Set champion spell points |
| `!health` | `!health [me\|opp] <N>` | Set champion health |

## Turn Phases

| Command | Usage | Description |
|---------|-------|-------------|
| `!pass` | `!pass` | Cycle through turn phases: FirstMainPhase → DeclareCombatPW → DeclareAttack → DeclareAttackPW → DeclareDefense → DeclareDefensePW → AssignFirstStrike → FirstStrikePW → AssignDamage → SecondMainPhase → EndPhase → Discard → EndTurn |
| `!phase` | `!phase <Name>` | Jump to a phase: `Mulligan`, `FirstMainPhase`, `DeclareAttack`, `EndTurn`, etc. |

## Card Zones

| Zone | Value | Description |
|------|-------|-------------|
| Deck | 1 | Face-down draw pile |
| Hand | 2 | Cards held by player |
| Champions | 4 | Champion cards |
| Warzone | 8 | Troops/artifacts in play |
| Discard | 16 | Destroyed/discarded cards |
| Void | 32 | Exiled/removed from game |
| PlayedResources | 64 | Shards played this turn |
| CastSpells | 128 | Spells on the stack |
| Underground | 256 | Tunneling troops |

## Card States (for `!state`)

| State | Value | Meaning |
|-------|-------|---------|
| None | 0 | Normal |
| Tapped | 1 | Exhausted (sideways) |
| Blocking | 2 | Currently blocking |
| Attacking | 4 | Currently attacking |
| Damaged | 16 | Took damage this turn |
| Healed | 32 | Healed this turn |
| Dead | 64 | Destroyed |
| HasAttacked | 128 | Attacked this turn |
| HasBlocked | 256 | Blocked this turn |
| EffectExpired | 512 | Temporary effect ended |
| Activated | 2048 | Activated ability used |

## Card Attributes (for `!attr`)

| Attr | Value | Effect |
|------|-------|--------|
| Flight | 2 | Can only be blocked by SkyGuard or Flight |
| Speed | 4 | Swiftstrike (deals damage first) |
| SkyGuard | 8 | Can block Flight |
| Crush | 16 | Excess damage carries to champion |
| Steadfast | 32 | Can't be killed by damage |
| Invincible | 64 | Can't be destroyed |
| SpellShield | 128 | Immune to enemy spells/abilities |
| Unique | 256 | Only one copy allowed |
| LifeDrain | 1 | Damage also heals champion |

## Utility

| Command | Usage | Description |
|---------|-------|-------------|
| `!help` | `!help` | Show all commands |
