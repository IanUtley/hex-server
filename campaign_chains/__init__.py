"""Campaign dungeon chain interface.

Extracted from campaign.py so that new dungeons can be added as separate
modules implementing ``ChainInterface`` — no merge conflicts.

See ``campaign.py`` for the existing campaign request handlers.
"""

from dataclasses import dataclass, field
from typing import Optional, Any

_CHAINS: dict[str, type] = {}


@dataclass
class ChainContext:
    """Context passed to every chain method."""
    camp_id: int
    champion_id: int
    state: dict[str, Any] = field(default_factory=dict)
    db: Any = None
    handler: Any = None


class ChainInterface:
    """A dungeon chain plugin. Subclass and implement these methods."""
    chain_name: str = ""
    races: list[str] = []

    def start_node(self, ctx: ChainContext) -> dict:
        raise NotImplementedError

    def advance_node(self, ctx: ChainContext, won: bool) -> dict:
        raise NotImplementedError

    def build_state(self, ctx: ChainContext) -> dict:
        raise NotImplementedError


def register_chain(arg=None):
    """Decorator. Register a dungeon chain for auto-discovery."""
    if isinstance(arg, type):
        _CHAINS[arg.chain_name] = arg
        return arg
    elif isinstance(arg, str):
        def deco(cls):
            _CHAINS[arg] = cls
            return cls
        return deco


def get_chain(name: str) -> Optional[type]:
    return _CHAINS.get(name)


def list_chains() -> list[str]:
    return list(_CHAINS.keys())
