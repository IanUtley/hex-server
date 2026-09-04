# Typed gamedata model

`Records/` is the extracted client gamedata snapshot. Each JSONL entry is an
outer JSON string containing a polymorphic serialized C# object. The inner
object identifies its concrete type in `_t`, serializer versions in `_v`, and
fields using the original `m_*` names.

The `gamedata` package preserves that shape while providing typed semantic
views:

```python
from gamedata import RecordStore, ability_graph

store = RecordStore()
graph = ability_graph(store, "5a8783b0-e420-4f41-b2a1-96f70b0cd851")
print(graph.costs)
print(graph.targets)
print(graph.effects)
```

`RecordStore` loads sections lazily and indexes records by GUID. Unknown
concrete C# types remain usable as `RecordObject` instances, with their full
fields and version map retained. Known mechanics contracts currently include
`AbilityTemplate`, target templates, effect templates, conditions, card
templates, effect-to-target mappings, and ability costs.

The semantic graph is deliberately separate from state mutation. `AbilityInstance`
and `PlayPlan` now provide the client-style activation boundary on top of it:

```python
from gamedata import ActivationData, PlayPlan, RecordStore

plan = PlayPlan.from_card(store, card_guid, source_uid=card_uid,
                          owner_id=player_id)
prompts = plan.required_prompts       # target/option/X/additional-cost input
activation = ActivationData.from_values(target_map={0: [target_uid]},
                                         option_map={0: 1}, x_cost=3)
# A card-play transaction can bind its shared wire selection to all graphs:
activations, cost_targets = plan.activation_bundle(
    [cost_uid, target_uid], x_cost=3)
```

`AbilityInstance` normalizes the protocol's target/option/variable maps, keeps
card-play abilities separate from triggered abilities, validates target and
payment counts plus required input, and orders effect groups by the same group/instance
metadata used by the client. The live resolver uses its runtime adapter for
that ordering without deserializing all Records files for every transaction.
`PlayPlan.activation_bundle()` partitions the client's single card-selection
list into authored card-cost targets and per-ability target-template indexes;
the resulting activation maps are persisted with the chain item and hydrated
when it resolves. `PlayPlan.cost_instances` exposes the same typed cost kind,
target template, and min/max bounds used to build the client's
`CostInstanceSessionEventArgs`.
Combat, priority, RNG, state mutation, and client event emission remain
runtime responsibilities rather than being guessed from localized card text.
The resolver and BOM walker use the current Records graph for effect wiring,
typed effect parameters, conditions, contingencies, and nested abilities. The
compact SQLite tables remain generated runtime indexes and persisted state,
not a second supported card-data version. There is no version-dispatch or
legacy-definition fallback: production resolution rejects an ability that is
absent from Records. Synthetic SQLite-only abilities are adapted in
`tests/tests_resolution.py` only.

## Version policy

The checked-in `Records/` snapshot is the only supported gamedata version.
Serializer `_v` entries are preserved for provenance and diagnostics, but they
are never used as type aliases or alternate execution paths. If the client
gamedata changes, regenerate the Records-derived indexes and update the typed
model in the same change.

Run the model checks with:

```text
python3 tests/tests_gamedata.py
```
