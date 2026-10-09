"""Records snapshot lookups that project ability, card and target data for the server."""

from functools import lru_cache
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from gamedata import AbilityGraph


def _records_ability_graph(handler, ability_guid) -> "AbilityGraph | None":
    """Resolve an ability through the current Records snapshot.

    A few focused tests use small handler doubles rather than a fully
    initialized HCPHandler.  They may provide the bound helper themselves;
    production handlers use their cached PlayPlan RecordStore.
    """
    resolver = getattr(handler, "_current_ability_graph", None)
    if callable(resolver):
        return cast(Any, resolver(ability_guid))
    from gamedata import DEFAULT_RECORD_STORE, ability_graph
    store = getattr(handler, "_play_plan_store", None)
    if store is None:
        store = DEFAULT_RECORD_STORE
        try:
            handler._play_plan_store = store
        except AttributeError:
            pass
    return ability_graph(store, str(ability_guid).lower())


@lru_cache(maxsize=8192)
def _records_card_projection(template_guid):
    """Return the immutable printed card projection from the Records cache.

    ``card_templates`` is a normalized database projection of the same client
    CardTemplate records.  CardUpdated is a hot path, so use the indexed,
    process-wide Records snapshot for static fields and leave SQLite to answer
    only the mutable game-card state.  A missing record is a normal fallback
    for champions, generated cards, and older/incomplete Records snapshots.
    """
    from gamedata import DEFAULT_RECORD_STORE

    guid = str(template_guid or "").lower()
    if not guid:
        return None
    record = DEFAULT_RECORD_STORE.get("CardTemplate", guid)
    if record is None:
        return None
    return (
        str(record.guid or guid).lower(),
        record.card_type,
        record.name,
        record.resource_cost,
        record.base_attack,
        record.base_defense,
        record.threshold_shards,
        tuple(record.ability_guids),
        record.attributes,
        record.subtype,
        record.lethal,
    )


def _records_target_spec(handler, target_guid):
    resolver = getattr(handler, "_current_target_spec", None)
    if callable(resolver):
        return resolver(target_guid)
    from gamedata import DEFAULT_RECORD_STORE
    store = (getattr(handler, "_play_plan_store", None)
             or DEFAULT_RECORD_STORE)
    target = store.get("AbilityTargetTemplate", str(target_guid).lower())
    return target.target_spec if target is not None else None
