"""Replenish Spell Power ability.

m_AbilityTemplateId: ccd5c608-671b-db65-bbb9-8b7276683fb1
[BASIC]: [3] [ARROWR] Gain 3[SP] to 5[SP] at random.
Cost: 3 charge points. Effect: gain 3-5 spell points.
"""

import random
import game_engine
from abilities.registry import register_custom_ability


@register_custom_ability("ccd5c608-671b-db65-bbb9-8b7276683fb1")
def replenish_spell_power(game, session, db, handler, pl_t, ai_t, bstate, ability_guid, source_scid):
    gain = random.randint(3, 5)
    bstate["player_spell_points"] = bstate.get("player_spell_points", 0) + gain
    game.player_spell_points = bstate["player_spell_points"]
    ev = game_engine.ChampionSpellPointsChangedSessionEventArgs()
    ev.player_id = pl_t
    ev.operation = 1
    ev.delta = gain
    ev.new_value = bstate["player_spell_points"]
    game._push(ev)
    return f"Replenish Spell Power: +{gain} SP (now {bstate['player_spell_points']})"
