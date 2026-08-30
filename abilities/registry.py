"""Ability registry — custom Python handlers for specific ability GUIDs.

Most abilities are data-driven through the ``ability_effects`` BOM (bill of
materials) table and don't need custom code.  Only special abilities that
can't be expressed in the BOM need a registered handler here.

Usage — adding a new card ability::

    # abilities/cards/my_new_ability.py
    from abilities.registry import register_custom_ability

    @register_custom_ability("some-ability-guid-here")
    def my_ability(game, session, db, handler, pl_t, ai_t, bstate, ability_guid, source_scid):
        ...
        return "log message"
"""

import importlib
import pkgutil
from pathlib import Path
from typing import Optional, Callable

_CUSTOM_ABILITIES: dict[str, Callable] = {}


def register_custom_ability(guid: str):
    """Decorator. Register a custom Python handler for a specific ability GUID.

    Place decorated functions in ``abilities/cards/`` — they are auto-discovered
    at startup.  One file per ability GUID avoids merge conflicts between
    contributors.
    """
    def deco(fn):
        _CUSTOM_ABILITIES[guid] = fn
        return fn
    return deco


def discover():
    """Auto-import all modules under ``abilities/cards/`` so their
    ``@register_custom_ability`` decorators fire.

    Called once at startup; subsequent calls are harmless no-ops.
    """
    cards_dir = Path(__file__).parent / "cards"
    for _, name, _ in pkgutil.iter_modules([str(cards_dir)]):
        importlib.import_module(f"abilities.cards.{name}")


def lookup(guid: str) -> Optional[Callable]:
    """Return the registered custom handler for *guid*, or ``None``."""
    return _CUSTOM_ABILITIES.get(guid)
