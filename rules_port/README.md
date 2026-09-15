# Python RulesPort runtime

`rules_port` is the Python implementation of the behavioral
contracts extracted from `HexClient/Game.Shared`. It is deliberately isolated
from transport and storage:

1. `kernel.py`, `phases.py`, `turn_states.py`, and `combat.py` own ordering,
   deterministic RNG, priority, chain, and combat identity.
2. `transactions.py`, `filters.py`, `targeting.py`, `targets.py`, `triggers.py`,
   and `abilities.py` model C# rule objects and their validation contracts.
3. `wire.py` accepts only an already-classified HConnect command plus typed
   nested payloads; it never guesses card IDs or targets from display text.
   The protocol parser exposes an opt-in `preserve_complex=True` mode for
   retaining nested UID/ResourceId fields while the typed decoder is migrated.
4. `async_bridge.py` turns client UI checkpoints into request/reply events.
5. `runtime_adapter.py` and `adapter.py` connect runtime facts, existing
   `pvp_db` mutation APIs, and the namespaced SQLite snapshot.
6. `resolution.py` owns the port ability lifecycle, typed effect walk, and
   continuation boundary. Legacy effect walking remains available only through
   the old direct APIs; RulesPort has no hybrid effect-backend injection path.
7. `tunneling.py` owns the StartTurn Tunneling counter advance and serialized
   Surface queue; it uses the native counter projection and port ability
   lifecycle in both Practice/PvE and tournament PvP.

## Runtime flow

Every battle request follows one direction through the port:

```text
HConnect ObjFmt
    -> classify_player_transaction (typed payload)
    -> rules_port.wire.normalize_player_transaction
    -> AuthoritativeSession requirements/phase/priority checks
    -> one RulesPort transaction resolver
    -> host projection (SQLite mutation + Game events)
    -> action-stack tick / continuation checkpoint
    -> one 3055 sync packet to Unity
```

The port owns rule decisions and ordering. A host projection only applies an
accepted decision to the runtime model and emits the client event contract. It
must not re-check cards by display text or silently choose a target. Card and
ability definitions come from `Records` through `AbilityGraph`; SQLite is the
mutable session projection. A request classified as a port intent is
acknowledged even when rejected and is never re-run through the legacy
dispatcher.

### UI checkpoints and continuations

The client is request-serialized: it will not send the next transaction until
the previous request has received a 3055 response. Rules that require input
pause the action stack and persist a continuation containing the ability
instance ID, source card, target/option maps, variables, and resume effect
order. The next typed `SetAbilityActivationData` (or discard, choice,
conversation, or triggered-ability response) resumes that same instance; it
does not create a second ability. `async_bridge.py` provides the equivalent
awaitable event-bus shape for non-Unity callers.

| Checkpoint | Port state | Client-visible response |
| --- | --- | --- |
| Opening hand / go first | `Mulligan` / `PickGoesFirst` | hand or choice dialog |
| Card or ability target | pending activation continuation | target/options event |
| Discard or sacrifice cost | cost target map | normal hand/cost picker |
| Choice/conversation | pending choice/conversation | class-specific dialog |
| Chain resolution | action-stack item + priority | chain item, green light, 3055 |

On completion, the resolver returns a continuation if another prompt is needed,
or the next priority event. A completed mutation must update the SQLite row and
emit the matching `CardMoved`/`CardUpdated` (and `CardDiscarded` for removal)
before options are rebuilt.

Player visibility is projected at the same boundary. A continuous modifier
such as Subterranean Spy persists `CanSeeOpponentsHand` in the battle
checkpoint; every fresh phase/priority packet then includes viewer-scoped full
`CardUpdated` definitions for the opposing hand. Face-down placeholder updates
for those same cards are suppressed so Unity cannot replace a revealed card
with a template-less black rectangle. Other viewers continue to receive the
normal nulling form.

### Resources and temporary grants

Resource effects are typed `CardModifier` operations. `currentresource`
changes only the controller's current/temporary pool; `totalresource` changes
the maximum pool; threshold and charge changes use their own events. The
resolver updates the persisted checkpoint and transient `Game` projection,
emits the pool event, and rebuilds main-phase options after the chain empties.
Thus Hideous Conversion's authored `Gain [L1][R0]` grants one temporary
resource to the current pool; it does not increase the maximum pool.

Payments and turn refills use the same native transition layer. The canonical
`player`/`ai` helpers serve Practice/PvE checkpoints, and raw player-ID helpers
serve the tournament projection; neither mode should decrement a cost by
writing checkpoint counters directly. `AbilityCostPlan` is also applied by a
RulesPort transition before the host projects its resource, charge, spell-point,
or life events.

The live HConnect host attaches one fully wired RulesPort session by default
(`HEX_RULES_PORT_AUTO_ATTACH=1`); `HEX_RULES_PORT_AUTO_ATTACH=0` is the explicit
rollback switch. A mode can also create one fully wired host with
`enable_rules_port(game_session, game, battle_state)`, or use
`rules_session_for(game_session, game)` when supplying adapters separately, then
feed classified commands through `submit_classified_transaction`, and compare
ordered events/state with `ParityCapture`. Records remain the static authority;
SQLite stores mutable runtime state and generated projections only.

Typed gameplay intents are normalized, validated, and consumed by the
RulesPort scheduler. Domain callbacks are projection adapters for SQLite and
Unity events; they do not select or implement gameplay rules. Typed requests
never fall through to the legacy dispatcher after a RulesPort rejection.
`DebugCheatTransaction` and `NonsenseTransaction` remain intentionally
outside the gameplay rules boundary. Live attached sessions use
`NativeEffectBackend`, `NativeTriggerBackend`, and the native target evaluator;
the deprecated compatibility adapters live outside the RulesPort dispatcher and
are not selectable by a live session.

Run `python3 tests/run_all.py --quick` to exercise the port alongside the
existing server tests. `coverage.py` exposes
`validate_transaction_coverage()` and `validate_effect_coverage()`, so additions
to the extracted C# transaction or effect inventories cannot silently bypass
the migration inventory. The effect validator distinguishes concrete handlers
from abstract or resolver-owned client templates.
