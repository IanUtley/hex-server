# campaign_chains/

Dungeon-chain extension interface for the campaign system.

## How it works

A dungeon chain is a class implementing `ChainInterface`. The decorator puts
the class in the in-process registry; a module must be imported by the active
campaign startup path before `get_chain()` can find it.

The existing campaign request handlers live in `campaign.py`.  This package
provides the **extension point** for new dungeon content.

## Adding a new dungeon

1. Create a new file, e.g. `campaign_chains/frostkeep.py`:

```python
from campaign_chains import ChainInterface, ChainContext, register_chain

@register_chain
class Frostkeep(ChainInterface):
    chain_name = "Frostkeep"
    races = ["Human", "Elf", "Dwarf"]

    def start_node(self, ctx: ChainContext) -> dict:
        """Return the first conversation node."""
        return {"node": "frostkeep_entrance", "text": "..."}

    def advance_node(self, ctx: ChainContext, won: bool) -> dict:
        """Advance after a battle. `won` is True if the player won."""
        if won:
            return {"node": "victory", "text": "..."}
        return {"node": "defeat", "text": "..."}

    def build_state(self, ctx: ChainContext) -> dict:
        """Return the full campaign GameplayState dict."""
        return {"ALoc": "frostkeep_entrance", ...}
```

2. Import the module from the campaign startup path so the decorator runs.

## ChainContext

Passed to every method — holds the session's `camp_id`, `champion_id`,
mutable `state` dict, and references to `db` and `handler`.

## Integration status

`campaign.py` remains the active implementation of the campaign request
handlers. This package is an extension point, not an automatic replacement
for those handlers: import a chain from the relevant startup/handler path
before expecting `get_chain(name)` to find it. Existing campaign data may also
need a `from_node` mapping in `campaign.py` when a new transition is added.
