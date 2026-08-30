"""Champion ability framework.

Most champion charge / spell powers are data-driven: their effects live in
the ``ability_effects`` table (BOM) and are executed by the leaf executors
in ``bom.py``.  Custom handlers go in ``abilities/cards/``.

This module provides champion-specific utilities that aren't leaf effects:
- Validating ability activation (charges, SP, threshold)
- Deducting costs and pushing update events
- Utility resolvers for common champion ability patterns.
"""

import json
import random

import game_engine
import battle_engine as _be

from ._shared import _log


# ── Activation helpers ──────────────────────────────────────────────────────

def validate_ability_cost(db, session, player_uid, ability_guid, bstate):
    """Check whether *player_uid* can pay for *ability_guid*.

    Returns (ok: bool, charge_cost: int, spell_cost: int, eff_sp_cost: int,
             missing_charges: int, missing_sp: int).
    """
    row = db.execute(
        "SELECT charge_cost, spell_cost FROM talent_abilities "
        "WHERE ability_guid=? LIMIT 1", (ability_guid,)).fetchone()
    if not row:
        return False, 0, 0, 0, 0, 0
    cc = row[0] or 0
    sc = row[1] or 0
    charges = bstate.get("player_charges", 0)
    sp = bstate.get("player_spell_points", 0)
    sp_uses = bstate.get("player_sp_uses", {}) or {}
    used = int(sp_uses.get(ability_guid, 0))
    eff_sc = sc + (used if sc > 0 else 0)
    return (charges >= cc and sp >= eff_sc, cc, sc, eff_sc,
            max(0, cc - charges), max(0, eff_sc - sp))


def apply_ability_cost(db, session, game, pl_t, player_uid, ability_guid,
                       bstate, player_champ_scid=None,
                       player_champ_guid=None):
    """Deduct charges and SP for an activated ability, push update events,
    increment the spell-power escalation counter, and return True on success.
    """
    ok, cc, sc, eff_sc, _, _ = validate_ability_cost(
        db, session, player_uid, ability_guid, bstate)
    if not ok:
        return False

    charges = bstate.get("player_charges", 0)
    sp = bstate.get("player_spell_points", 0)
    sp_uses = bstate.get("player_sp_uses", {}) or {}

    bstate["player_charges"] = charges - cc
    bstate["player_spell_points"] = sp - eff_sc

    if sc > 0:
        sp_uses[ability_guid] = sp_uses.get(ability_guid, 0) + 1
        bstate["player_sp_uses"] = sp_uses

    # Push deduction events
    if cc:
        ev = game_engine.ChampionChargePointsChangedSessionEventArgs()
        ev.player_id = pl_t; ev.operation = 2; ev.delta = cc
        ev.new_value = bstate["player_charges"]; game._push(ev)
    if eff_sc:
        ev = game_engine.ChampionSpellPointsChangedSessionEventArgs()
        ev.player_id = pl_t; ev.operation = 2; ev.delta = eff_sc
        ev.new_value = bstate["player_spell_points"]; game._push(ev)

    # Reflect escalated SP cost on champion card
    if player_champ_scid and sp_uses.get(ability_guid):
        cdef = game.card_defs.get(player_champ_scid)
        if cdef is not None:
            for ag, uses in sp_uses.items():
                if int(uses) > 0:
                    cdef.spell_point_cost_mods[
                        game_engine.ResourceId.from_str(ag)] = int(uses)
            game.push_card_updated(
                player_champ_scid, pl_t,
                game_engine.ECardCollections.Champions,
                game_engine.ECardTypes.Champion,
                template_id=player_champ_guid)

    return True


# ── Common champion ability patterns ────────────────────────────────────────

def heal_self(game, pl_t, amount, bstate, key="player_health"):
    """Add *amount* health to the player, capped at max (default 25)."""
    max_hp = 25
    current = bstate.get(key, max_hp)
    new_val = min(current + amount, max_hp)
    bstate[key] = new_val
    ev = game_engine.ChampionHealthChangedSessionEventArgs()
    ev.player_id = pl_t
    ev.operation = 1
    ev.delta = amount
    ev.new_value = new_val
    game._push(ev)
    return f"heal {amount} HP -> {new_val}"


def damage_self(game, pl_t, amount, bstate, key="player_health"):
    """Subtract *amount* health from the player (no minimum)."""
    current = bstate.get(key, 20)
    new_val = max(0, current - amount)
    bstate[key] = new_val
    ev = game_engine.ChampionHealthChangedSessionEventArgs()
    ev.player_id = pl_t
    ev.operation = 2
    ev.delta = amount
    ev.new_value = new_val
    game._push(ev)
    return f"self-damage {amount} HP -> {new_val}"


def gain_resource(game, pl_t, bstate, shard_color, key="player_resources_current"):
    """Add 1 to the player's current resource count for *shard_color*."""
    import struct
    colors = {
        "Blood": 4, "Ruby": 8, "Sapphire": 16,
        "Wild": 32, "Diamond": 64, "Colorless": 1,
    }
    flag = colors.get(shard_color, 0)
    current = bstate.get(key, 0)
    bstate[key] = current + 1
    ev = game_engine.PlayerTotalResourcePoolChangedSessionEventArgs()
    ev.player_id = pl_t; ev.operation = 1; ev.delta = 1
    ev.new_value = bstate[key]
    game._push(ev)
    return f"gain resource ({shard_color}) -> {bstate[key]}"


def draw_cards(game, session, handler, pl_t, ai_t, bstate, count=1):
    """Draw *count* cards for the player."""
    for _ in range(count):
        handler._player_draw_card(game, session, pl_t)
    return f"draw {count} card(s)"


def random_sp_gain(game, pl_t, bstate, low=3, high=5, key="player_spell_points"):
    """Gain a random amount of spell points between *low* and *high*."""
    gain = random.randint(low, high)
    current = bstate.get(key, 0)
    bstate[key] = current + gain
    game.player_spell_points = bstate[key]
    ev = game_engine.ChampionSpellPointsChangedSessionEventArgs()
    ev.player_id = pl_t; ev.operation = 1; ev.delta = gain
    ev.new_value = bstate[key]
    game._push(ev)
    return f"gain {gain} SP -> {bstate[key]}"


def stack_push_ability(bstate, game, ability_guid, source_scid):
    """Push an ability onto the chain (client stack) for later resolution."""
    champ_uid = source_scid.uid.to_uint64() if hasattr(source_scid, 'uid') else 0
    _be.stack_push(bstate, {
        "kind": "ability",
        "ability_guid": str(ability_guid),
        "source_uid": champ_uid,
        "instance_id": 1,
    })
    game.push_ability_on_chain(
        source_scid,
        game_engine.ResourceId.from_str(str(ability_guid)))
    return f"pushed ability {str(ability_guid)[:8]} onto chain"
