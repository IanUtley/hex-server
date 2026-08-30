"""Champion ability effects (compatibility re-exports).

All implementation has moved into the ``abilities`` package.
New code should import from ``abilities`` directly.

To add a custom card ability, create a file in ``abilities/cards/`` and use
``@register_custom_ability`` from ``abilities.registry``.
"""

from abilities import (
    resolve_effect,
    resolve_played_spell,
    discover_abilities,
    kill_troop,
    state_based_deaths,
    transform_card,
    resolve_deathcry,
    _resolve_deathcry_effect,
    apply_card_stat_mod,
    apply_pregame_abilities,
    decode_tac,
    tac_guid,
    tac_function,
    bom_has_discard,
    bom_has_leaf,
    resolve_triggers,
    resolve_enters_play_triggers,
    resolve_stack_trigger,
    _stat_delta,
    register_custom_ability,
    register_condition,
    evaluate_condition,
)

from abilities.framework.bom import _walk_bom, _LEAFS as _leafs_module
from abilities.framework._shared import _log

# Re-exports needed by hconnect_server that still reference ability.*
_EFFECTS = {}  # moved to abilities.registry

from abilities.cards.replenish_spell_power import replenish_spell_power
