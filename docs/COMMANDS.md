# Chat Command Reference

Commands accept either the historical `!` prefix or `/` prefix. Type in chat
(any tab works after session room join). `thresholds` is accepted as an alias
for `threshold`.
The server accepts these commands only when `allowcon` is present in
`HEX_PROFILE_FLAGS`.

The status column reflects the server command router in `commands.py`.
“Implemented” means the command is accepted and has a server handler; it may
still require an active game/session as noted below. The `!help` output is the
authoritative in-game list.

## Session & Test Controls

| Command | Usage | Status | Description |
|---------|-------|--------|-------------|
| `!game_end` | `!game_end victory\|defeat` | Implemented | End the campaign battle with a test win or loss; available outside an active game. |
| `!encounter` | `!encounter <name>` | Implemented | Start a named campaign encounter; available outside an active game. |
| `!challenge` | `!challenge [opponent]` | Implemented | Create a duel challenge. |
| `!reload` | `!reload` | Implemented | Reload runtime modules. |

## Card Info & Manipulation

| Command | Usage | Status | Description |
|---------|-------|--------|-------------|
| `!hand` | `!hand` | Implemented | List cards in hand: `CardName [card_id]` |
| `!aihand` | `!aihand` | Implemented | Reveal the AI hand to the player for debugging. |
| `!zones` | `!zones` | Implemented | List all cards grouped by zone (one message per zone) |
| `!playable` | `!playable [id\|name ...]` | Implemented | Set which cards get golden outlines. No args = all playable. |
| `!drawcard` | `!drawcard <id\|name> [id\|name ...]` | Not implemented | No server handler currently exists; use `!draw N` or `!addcard <name\|id>`. |
| `!draw` | `!draw N` | Implemented | Draw N cards from deck. |
| `!gencard` | `!gencard <name>` | Implemented | Generate a copy of a card template into hand. |
| `!addcard` | `!addcard <name\|id>` | Implemented | Draw the next matching copy from the deck to hand. |
| `!top` | `!top <id\|name>` | Implemented | Move a card from hand to the top of the deck. |
| `!discard` | `!discard` | Implemented | Discard a random card from hand. |
| `!move` | `!move <card_id> <zone>` | Implemented | Move card: `deck`, `hand`, `warzone`, `discard`, `void`, `playedresources`, `underground` |
| `!update` | `!update <card_id>` | Implemented | Resend CardUpdated event for a card (restores thresholds/gems). |

## Card State & Attributes

| Command | Usage | Status | Description |
|---------|-------|--------|-------------|
| `!state` | `!state <id> <flags>` | Implemented | Set card state: `Tapped\|Attacking\|Blocking\|Damaged\|Healed\|Dead\|HasAttacked\|HasBlocked\|EffectExpired\|Activated` |
| `!attr` | `!attr <id> <flags>` | Implemented | Set attributes: `Flight\|Speed\|SkyGuard\|Crush\|Steadfast\|Invincible\|SpellShield\|Unique\|LifeDrain` |

Flags can be pipe-separated (`Tapped\|Attacking`) or space-separated. Unknown flags are rejected without changes.

## Resources & Champion

| Command | Usage | Status | Description |
|---------|-------|--------|-------------|
| `!threshold` | `!threshold [me\|opp] C B R S W D` | Implemented | Set 6 threshold counts (Colorless Blood Ruby Sapphire Wild Diamond). `!thresholds` is an alias. |
| `!resource` | `!resource [me\|opp] <current> <maximum>` | Implemented | Set current and maximum resources. `!resources` is an alias. |
| `!charge` | `!charge [me\|opp] <N>` | Implemented | Set champion charges. |
| `!spellpoints` | `!spellpoints [me\|opp] <N>` | Implemented | Set champion spell points. |
| `!health` | `!health [me\|opp] <N>` | Implemented | Set champion health. |

## Turn Phases

| Command | Usage | Status | Description |
|---------|-------|--------|-------------|
| `!pass` | `!pass` | Implemented | Cycle through turn phases: FirstMainPhase → DeclareCombatPW → DeclareAttack → DeclareAttackPW → DeclareDefense → DeclareDefensePW → AssignFirstStrike → FirstStrikePW → AssignDamage → SecondMainPhase → EndPhase → Discard → EndTurn |
| `!phase` | `!phase <Name>` | Implemented | Jump to a phase: `Mulligan`, `FirstMainPhase`, `DeclareAttack`, `EndTurn`, etc. |

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

| Command | Usage | Status | Description |
|---------|-------|--------|-------------|
| `!help` | `!help` | Implemented | Show the command list. |
