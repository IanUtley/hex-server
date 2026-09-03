"""BOM (Bill of Materials) walking and leaf executors for champion abilities.

A champion ability is a bill-of-materials: the ``ability_effects`` table expands
it into an ordered list of leaf effect templates.  Each leaf's ``effect_type``
maps to an executor registered via ``@leaf_register``.
"""

import json
import inspect
import struct
import random

import game_engine

from ._shared import (_log, next_game_card_uid, owner_uid, pvp_champion_uid,
                      pvp_opponent_pid, state_after_zone_exit)
from .effects.damage import deal_damage
from .effects.registry import _LEAFS, leaf_register
from .effects.tokens import summon_token, conscript_cards, load_player_deck
from .effects.counters import card_counters
from .effects import utility as _utility  # register generic client effects
from .fields import (ability_record, effect_field, effect_template,
                     effect_template_value, modifier_metadata)


def _walk_bom(db, ability_guid):
    """Return ordered BOM rows (dicts) for an ability from ability_effects."""
    rows = db.execute(
        "SELECT effect_guid, effect_type, param FROM ability_effects "
        "WHERE ability_guid=? ORDER BY effect_order",
        (ability_guid,)).fetchall()
    if not rows:
        # A granted sub-ability can be absent from the compact normalized seed
        # when it is only reachable through GrantAbility.  Recover its BOM
        # from the extracted AbilityTemplate and typed effect templates.
        from .fields import ability_record, effect_template
        record = ability_record(db, ability_guid)
        for entry in record.get("m_AbilityEffectList") or []:
            if not isinstance(entry, dict):
                continue
            effect = entry.get("m_EffectTemplateId") or {}
            effect_guid = str(effect.get("m_Guid") or "").lower()
            template = effect_template(effect_guid) or {}
            effect_type = str(template.get("_t") or "").rsplit(".", 1)[-1]
            param = ""
            if effect_type == "ActivateAbilityEffectTemplate":
                param = str((template.get("m_AbilityToInvoke") or {}).get(
                    "m_Guid") or "").lower()
            elif effect_type == "GrantAbilityEffectTemplate":
                param = str((template.get("m_GrantedAbilityTemplateId") or {}).get(
                    "m_Guid") or "").lower()
            elif effect_type == "CardModifierAbilityEffectTemplate":
                modifier = template.get("m_Modifier") or {}
                kind = str(modifier.get("_t") or "").rsplit(".", 1)[-1]
                prop = {"DamageModifier": "damage",
                        "AttackModifier": "attack",
                        "DefenseModifier": "defense"}.get(kind, "")
                param = json.dumps({"property": prop,
                                    "amount": 0,
                                    "duration": entry.get("m_EffectDuration",
                                                             "Instant")})
            rows.append((effect_guid, effect_type, param))
    return [{"effect_guid": r[0], "effect_type": r[1] or "", "param": r[2] or ""} for r in rows]


def _deck_owner_for_target(db, handler, session, bstate, target):
    """Resolve a target champion to the DB owner of that champion's deck."""
    if target is None:
        return None
    row = db.execute(
        "SELECT user_id FROM game_cards WHERE session_id=? AND card_uid=?",
        (session.session_id, int(target))).fetchone()
    if row:
        return int(row[0])
    if (bstate or {}).get("pvp"):
        # PvP champion cards are represented by champ_map rather than rows in
        # game_cards.  Both pids are nonzero, so do not collapse the opponent
        # onto the local player's UID.
        for pid, champ_uid in ((bstate or {}).get("champ_map") or {}).items():
            try:
                if int(champ_uid) == int(target):
                    return int(pid)
            except (TypeError, ValueError):
                continue
    p = getattr(handler, "_player_champ_scid", None)
    a = getattr(handler, "_ai_champ_scid", None)
    if p is not None and int(target) == int(p.uid.uid64):
        return (handler.user_profile["id"] if handler.user_profile else 0)
    if a is not None and int(target) == int(a.uid.uid64):
        return 0
    return None


# ---------------------------------------------------------------------------
#  Leaf executors
# ---------------------------------------------------------------------------

@leaf_register("DrawNCardsAbilityEffectTemplate")
def _leaf_draw(game, session, db, handler, pl_t, ai_t, bstate, effect_guid, param):
    """Draw for the resolved target (Oracle Song: "Target champion draws 2
    cards") or the caster when there is no target."""
    import re as _re
    count = 1
    text = _ability_text(db, bstate) or ""
    m = _re.search(r'draw[s]?\s+(\w+)\s+card', text.lower())
    if m:
        g = m.group(1)
        words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                 "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
        count = words.get(g, int(g) if g.isdigit() else count)
    if param:
        try:
            d = json.loads(param)
            count = int(d.get("count", count))
        except Exception:
            pass
    typed_count = effect_field(
        db, bstate, effect_guid, "m_InputValue", default=0)
    if typed_count > 0:
        count = typed_count
    target = (bstate or {}).get("player_spell_target")
    owner = None
    if target is not None:
        owner = _deck_owner_for_target(db, handler, session, bstate, target)
    for _ in range(max(1, count)):
        if owner == 0:
            import ai as _ai
            _ai.ai_draw_card(handler, game, session, ai_t, bstate)
        else:
            draw_uid = (pl_t if owner is None
                        else owner_uid(owner, pl_t, ai_t, bstate))
            # Older focused harnesses expose the three-argument Practice
            # helper; the live/PvP handler also accepts the deck owner.  Keep
            # the leaf independent of that adapter detail.
            draw_fn = handler._player_draw_card
            try:
                accepts_owner = len(inspect.signature(draw_fn).parameters) >= 4
            except (TypeError, ValueError):
                accepts_owner = True
            if accepts_owner:
                draw_fn(game, session, draw_uid, owner)
            else:
                draw_fn(game, session, draw_uid)
    return f"draw {count} for owner {owner}"


@leaf_register("PutTopOfDeckIntoHandAbilityEffectTemplate")
def _leaf_put_top_into_hand(game, session, db, handler, pl_t, ai_t, bstate, effect_guid, param):
    """Put the top card(s) of the target champion's deck into the caster's
    hand (c3d824ce: "Put the top ESC:1 card of target opposing champion's deck
    into your hand")."""
    count = 1
    ability_guid = (bstate or {}).get("resolving_ability", "")
    if ability_guid:
        raw_row = db.execute(
            "SELECT raw_json FROM card_abilities_meta WHERE ability_guid=?",
            (ability_guid,)).fetchone()
        if raw_row and raw_row[0]:
            from .statics import _variable_value
            src = (bstate or {}).get("resolving_source_uid")
            v = _variable_value(
                db, session.session_id, bstate, raw_row[0], "amount",
                (bstate or {}).get("resolving_owner_id", 0),
                int(src) if src else 0)
            if v is not None:
                count = int(v)
    target = (bstate or {}).get("player_spell_target")
    deck_owner = None
    if target is not None:
        deck_owner = _deck_owner_for_target(db, handler, session, bstate, target)
    if deck_owner is None:
        deck_owner = 0
    moved = 0
    for _ in range(max(0, count)):
        rows = db.execute(
            "SELECT id, card_uid, card_template_id, template_guid FROM game_cards "
            "WHERE session_id=? AND user_id=? AND location='deck' ORDER BY position LIMIT 1",
            (session.session_id, deck_owner)).fetchall()
        if not rows:
            break
        row = rows[0]
        scid = game_engine.SessionCardId(game_engine.UID(row[1]))
        hand_owner = (int(pl_t.uid64) >> 8
                      if (bstate or {}).get("pvp")
                      else handler.user_profile["id"])
        db.execute(
            "UPDATE game_cards SET user_id=?, location='hand', position=100 "
            "WHERE id=?",
            (hand_owner, row[0]))
        db.commit()
        tpl_guid, ct, name, cost, atk, def_, gem = handler._card_full_data(
            game, scid, row[3], row[2])
        game.push_card_moved(scid, pl_t, game_engine.ECardCollections.Hand,
                             game_engine.ECardLocations.Top, 1)
        game.push_card_drawn(scid, pl_t, 1)
        game.push_card_updated(scid, pl_t, game_engine.ECardCollections.Hand,
                               ct, attack=atk, defense=def_, cost=cost,
                               template_id=tpl_guid, gems=gem)
        moved += 1
    return f"put {moved} deck card(s) into hand"


@leaf_register("DiscardCardAbilityEffectTemplate")
def _leaf_discard(game, session, db, handler, pl_t, ai_t, bstate, effect_guid, param):
    return "discard a card (paid as activation cost)"


@leaf_register("RandomizeVariableAbilityEffectTemplate")
def _leaf_randomize(game, session, db, handler, pl_t, ai_t, bstate, effect_guid, param):
    from .cards.replenish_spell_power import replenish_spell_power
    return replenish_spell_power(game, session, db, handler, pl_t, ai_t, bstate, effect_guid, None)


def _champion_target_uid(handler, bstate, db, session):
    """When an ability's first target template is a PlayerTargetTemplate
    ('You' — the controller's champion, e.g. Shamed Gladiator's Deploy "This
    deals 2 damage to you"), return that champion's SessionCardId uid — the
    effect hits the champion, not the source card.  Data-driven from the
    template's gamedata kind."""
    ag = (bstate or {}).get("resolving_ability")
    if not ag:
        return None
    row = db.execute(
        "SELECT target_template_ids FROM card_abilities_meta WHERE ability_guid=?",
        (ag,)).fetchone()
    if not row or not row[0]:
        return None
    try:
        tids = json.loads(row[0])
    except Exception:
        return None
    if not tids:
        return None
    trow = db.execute(
        "SELECT target_kind FROM target_templates WHERE template_id=?",
        (tids[0],)).fetchone()
    if not trow or (trow[0] or "") != "PlayerTargetTemplate":
        return None
    owner = (bstate or {}).get("resolving_owner_id")
    if owner is None:
        owner = (bstate or {}).get("resolving_source_uid")
        if owner is not None:
            orow = db.execute(
                "SELECT user_id FROM game_cards WHERE session_id=? AND card_uid=?",
                (session.session_id, int(owner))).fetchone()
            owner = orow[0] if orow else 0
    if (bstate or {}).get("pvp"):
        champ_uid = pvp_champion_uid(bstate, owner)
        return int(champ_uid) if champ_uid is not None else None
    champ = (getattr(handler, "_player_champ_scid", None) if owner
             else getattr(handler, "_ai_champ_scid", None))
    if champ is None:
        return None
    return int(champ.uid.uid64)


def _opposing_champion_uid(handler, bstate, db, session):
    """"This deals N damage to each opposing champion" — the effect's gamedata
    AbilityTargetTemplate declares MultiplePlayers / "each opposing champion";
    the champion opposite the ability source's controller is the target."""
    import json as _j
    ag = (bstate or {}).get("resolving_ability", "")
    if not ag:
        return None
    row = db.execute(
        "SELECT target_template_ids FROM card_abilities_meta "
        "WHERE ability_guid=?", (ag,)).fetchone()
    if not row or not row[0]:
        return None
    try:
        tids = _j.loads(row[0])
    except Exception:
        return None

    def _has_filter(node, wanted):
        if isinstance(node, dict):
            if str(node.get("_t", "")).rsplit(".", 1)[-1] == wanted:
                return True
            return any(_has_filter(value, wanted)
                       for value in node.values())
        if isinstance(node, list):
            return any(_has_filter(value, wanted) for value in node)
        return False

    for tid in (tids or []):
        trow = db.execute(
            "SELECT game_text, player_filter, filter_json FROM target_templates "
            "WHERE template_id=?", (tid,)).fetchone()
        if not trow:
            continue
        try:
            target_filter = _j.loads(trow[2] or "{}")
        except (TypeError, ValueError, _j.JSONDecodeError):
            target_filter = {}
        # MultiplePlayers alone is not enough: it can describe a target pool
        # containing troops and champions.  The typed filter must identify a
        # champion and exclude the source controller.  The localized phrase
        # remains only for pre-metadata rows.
        opposing_champion = (
            _has_filter(target_filter, "IsHero") and
            (_has_filter(target_filter, "IsNotControlledBy") or
             str(trow[1] or "").lower() in ("opponent", "opposing")))
        legacy_opposing = (not trow[2] and
                           "each opposing champion" in
                           (trow[0] or "").lower())
        if opposing_champion or legacy_opposing:
            owner = (bstate or {}).get("resolving_owner_id", 0)
            if (bstate or {}).get("pvp"):
                opponent_pid = pvp_opponent_pid(bstate, owner)
                champ_uid = pvp_champion_uid(bstate, opponent_pid)
                return int(champ_uid) if champ_uid is not None else None
            if owner:
                a = getattr(handler, "_ai_champ_scid", None)
                return int(a.uid.uid64) if a else None
            p = getattr(handler, "_player_champ_scid", None)
            return int(p.uid.uid64) if p else None
    return None


def _apply_resource_property(game, session, db, handler, pl_t, ai_t, bstate,
                             pm, target_uid):
    """Apply resource / charge / threshold CardModifier leaves
    ("Each champion gains 10 [DIAMOND]", Demolition's "[L-1][R-1]").  The
    affected side(s) come from the resolved target champion, or both when the
    text says "each champion"."""
    import re as _re
    text = (pm.get("text") or "").lower()
    amount = int(pm.get("amount") or 0)
    # ChargePointsModifier (and other typed modifiers) stores its operand as
    # an EffectInputVariable (usually the ability constant ``A``), so the
    # extracted parent param quite correctly has amount=0.  Resolve that
    # operand from the ability metadata instead of treating zero as a no-op.
    if amount == 0:
        raw_row = db.execute(
            "SELECT raw_json FROM card_abilities_meta WHERE ability_guid=?",
            ((bstate or {}).get("resolving_ability", ""),)).fetchone()
        raw = raw_row[0] if raw_row else ""
        if not raw:
            raw = json.dumps(ability_record(
                db, (bstate or {}).get("resolving_ability", "")))
        from .statics import _leaf_numeric_value
        amount = int(_leaf_numeric_value(
            db, session.session_id, bstate, pm, raw,
            (bstate or {}).get("resolving_owner_id", 0),
            int((bstate or {}).get("resolving_source_uid") or 0),
            pm.get("property") or "") or 0)
    sides = []
    if "each champion" in text or "each opposing champion" in text:
        sides = ["player", "ai"]
    else:
        owner = None
        if target_uid is not None:
            row = db.execute(
                "SELECT user_id FROM game_cards WHERE session_id=? AND card_uid=?",
                (session.session_id, int(target_uid))).fetchone()
            if row:
                owner = row[0]
            else:
                p = getattr(handler, "_player_champ_scid", None)
                a = getattr(handler, "_ai_champ_scid", None)
                if p is not None and int(target_uid) == int(p.uid.uid64):
                    owner = (handler.user_profile["id"]
                             if handler.user_profile else 0)
                elif a is not None and int(target_uid) == int(a.uid.uid64):
                    owner = 0
        if owner is None:
            owner = (bstate or {}).get("resolving_owner_id", 0) or \
                    (handler.user_profile["id"] if handler.user_profile else 0)
        sides = ["player" if owner else "ai"]
    logs = []
    prop = pm.get("property")
    color_flag = 0
    if prop == "threshold":
        m = _re.search(r'\[([A-Za-z]+)\]', text)
        if m:
            color_flag = game_engine.SHARD_TO_FLAG.get(m.group(1).lower(), 0)
    for side in sides:
        if prop == "currentresource":
            key = f"{side}_resources"
            cur = int(bstate.get(key, 0))
            bstate[key] = max(0, cur + amount)
            ev = game_engine.PlayerCurrentResourcePoolChangedSessionEventArgs()
            ev.player_id = pl_t if side == "player" else ai_t
            ev.operation = 1 if amount >= 0 else 2
            ev.delta = amount
            ev.new_value = bstate[key]
            game._push(ev)
            logs.append(f"{side} resources {cur}->{bstate[key]}")
        elif prop == "chargepoints":
            key = f"{side}_charges"
            cur = int(bstate.get(key, 0))
            bstate[key] = max(0, cur + amount)
            if side == "player":
                game.player_charges = bstate[key]
            else:
                game.ai_charges = bstate[key]
            ev = game_engine.ChampionChargePointsChangedSessionEventArgs()
            ev.player_id = pl_t if side == "player" else ai_t
            ev.operation = 1 if amount >= 0 else 2
            ev.delta = amount
            ev.new_value = bstate[key]
            game._push(ev)
            logs.append(f"{side} charges {cur}->{bstate[key]}")
        elif prop == "totalresource":
            key = f"{side}_total_resources"
            cur = int(bstate.get(key, 0))
            bstate[key] = max(0, cur + amount)
            ev = game_engine.PlayerTotalResourcePoolChangedSessionEventArgs()
            ev.player_id = pl_t if side == "player" else ai_t
            ev.operation = 1 if amount >= 0 else 2
            ev.delta = amount
            ev.new_value = bstate[key]
            game._push(ev)
            logs.append(f"{side} total {cur}->{bstate[key]}")
        elif prop == "threshold" and color_flag:
            key = f"{side}_threshold"
            th = bstate.setdefault(key, {})
            cur = int(th.get(color_flag, 0))
            th[color_flag] = max(0, cur + amount)
            ev = game_engine.PlayerResourceThresholdChangedSessionEventArgs()
            ev.player_id = pl_t if side == "player" else ai_t
            ev.color = color_flag
            ev.operation = 1 if amount >= 0 else 2
            ev.delta = amount
            ev.new_value = th[color_flag]
            game._push(ev)
            logs.append(f"{side} threshold {color_flag} {cur}->{th[color_flag]}")
    return "; ".join(logs)


@leaf_register("CardModifierAbilityEffectTemplate")
def _leaf_card_modifier(game, session, db, handler, pl_t, ai_t, bstate, effect_guid, param):
    """CardModifier handles heal, damage, stat changes for champion powers.

    Parses the AbilityEffectTemplate name to determine the modifier type
    (e.g. 'Gain5Health' → heal 5 HP, 'M1Atk' → -1 ATK, 'YouLose4Health' → damage 4).
    """
    import re as _re
    from .champions import heal_self, damage_self
    from .stat_mod import apply_card_stat_mod
    from .effects.counters import (
        add_card_counter, remove_card_counters, counter_name_from_text,
    )
    from ._shared import (
        apply_attribute_grant,
        owner_uid, card_collection_for_location, state_after_zone_exit,
    )

    # Data-driven path: the leaf's parent-level param JSON carries
    # {property, amount, duration} resolved from the top-level ability record.
    pm = _parse_leaf_param(param)
    typed_modifier = modifier_metadata(effect_guid)
    if typed_modifier:
        # Parent params from older extractions remain useful for duration and
        # target wiring, while the child effect template is authoritative for
        # the operation and modifier-specific fields.
        pm = dict(pm or {})
        if typed_modifier.get("property"):
            pm.setdefault("property", typed_modifier["property"])
        if typed_modifier.get("input_value") and not pm.get("amount"):
            pm["amount"] = typed_modifier["input_value"]
        if typed_modifier.get("attributeflags"):
            pm["attribute_flags"] = typed_modifier["attributeflags"]
        if typed_modifier.get("attribute"):
            pm["attribute"] = typed_modifier["attribute"]
        if typed_modifier.get("operation"):
            pm["operation"] = typed_modifier["operation"]
        if typed_modifier.get("counter_template_guid"):
            pm["counter_template_guid"] = typed_modifier[
                "counter_template_guid"]
        for key in ("removeallcounters", "removehalfroundedup",
                    "replaceexistingvalue"):
            if key in typed_modifier:
                pm[key] = typed_modifier[key]
    if pm and pm.get("property") in ("attack", "defense", "healhero",
                                      "attribute", "counter", "damage",
                                      "currentresource", "totalresource",
                                      "threshold", "chargepoints", "cardcost",
                                      "intattr"):
        target_uid = ((bstate or {}).get("player_mod_target")
                      or (bstate or {}).get("player_spell_target"))
        # Resolve numeric values from the ability's serialized variables.  In
        # particular, CounterVariable and TriggerTargetPropertyVariable are
        # not literal values even when the effect row carries amount=0 or 1.
        raw_row = db.execute(
            "SELECT raw_json FROM card_abilities_meta WHERE ability_guid=?",
            ((bstate or {}).get("resolving_ability", ""),)).fetchone()
        raw = raw_row[0] if raw_row else ""
        if not raw:
            raw = json.dumps(ability_record(
                db, (bstate or {}).get("resolving_ability", "")))
        src_uid = (bstate or {}).get("resolving_source_uid")
        src_owner = (bstate or {}).get("resolving_owner_id", 0)

        def _numeric(prop):
            from .statics import _leaf_numeric_value
            return _leaf_numeric_value(
                db, session.session_id, bstate, pm, raw, src_owner,
                int(src_uid) if src_uid is not None else 0, prop)

        try:
            _raw_vars = json.loads(raw or "{}").get("m_Variables") or []
        except (TypeError, ValueError, json.JSONDecodeError):
            _raw_vars = []
        has_dynamic_numeric = any(
            str(v.get("_t", "")).split(".")[-1] in (
                "CounterVariable", "TriggerTargetPropertyVariable",
                "ExpressionAbilityVariable", "CardSumAbilityVariable",
                "CardCountAbilityVariable", "CountListAttrAbilityVariable")
            for v in _raw_vars if isinstance(v, dict))

        if pm.get("property") == "intattr":
            if target_uid is None:
                return "intattr: no target"
            # IntAttrModifier fields are typed metadata.  The normalized
            # parent param often carries amount=0, so use the child
            # m_Value/operation supplied by modifier_metadata instead of
            # inferring the marker from localized game text.
            attr = str(pm.get("attribute") or "")
            operation = str(pm.get("operation") or "Set").lower()
            amount = pm.get("amount")
            if (amount is None or int(amount or 0) == 0) and typed_modifier:
                amount = typed_modifier.get("value", 0)
            try:
                amount = int(amount or 0)
            except (TypeError, ValueError):
                amount = 0
            if not attr:
                return "intattr: missing attribute"
            row = db.execute(
                "SELECT template_guid, user_id, location, card_state, "
                "permanent_buffs FROM game_cards WHERE session_id=? "
                "AND card_uid=?", (session.session_id, int(target_uid))
            ).fetchone()
            if not row:
                return f"intattr: target {hex(int(target_uid))} missing"
            try:
                saved = json.loads(row[4] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                saved = {}
            if not isinstance(saved, dict):
                saved = {}
            markers = saved.setdefault("int_attrs", {})
            if not isinstance(markers, dict):
                markers = {}
                saved["int_attrs"] = markers
            current = int(markers.get(attr, 0) or 0)
            if operation in ("add", "increment"):
                value = current + amount
            elif operation in ("remove", "subtract"):
                value = current - amount
            else:
                value = amount
            if value:
                markers[attr] = value
            else:
                markers.pop(attr, None)
            db.execute(
                "UPDATE game_cards SET permanent_buffs=? WHERE session_id=? "
                "AND card_uid=?", (json.dumps(saved), session.session_id,
                                    int(target_uid)))
            db.commit()
            # Tamed and Untamed markers are mutually exclusive.  Keep the
            # original static Untamed ability attached for metadata/aura
            # purposes, but suppress its effective marker once Tamed is set.
            tamed_success = attr.lower() == "tamed" and value > 0
            if tamed_success:
                markers.pop("Untamed", None)
                # A successful Taming Sphere capture exiles the captured
                # troop.  This is deliberately keyed from the typed
                # IntAttrModifier (Tamed=1), rather than a card name or game
                # text, so both the 2-cost chance branch and the 5-cost
                # guaranteed branch behave identically.  Failed random
                # branches never apply this modifier and therefore leave the
                # target in its original zone.
                db.execute(
                    "UPDATE game_cards SET permanent_buffs=?, location='void', "
                    "position=0, card_state=? WHERE session_id=? "
                    "AND card_uid=?", (json.dumps(saved),
                                       state_after_zone_exit(row[3]),
                                       session.session_id,
                                       int(target_uid)))
                db.commit()
            scid = game_engine.SessionCardId(game_engine.UID(int(target_uid)))
            _tpl, ct, _name, cost, atk, defense, gem = handler._card_full_data(
                game, scid, row[0])
            cdef = game.card_defs.get(scid)
            owner = owner_uid(row[1], pl_t, ai_t, bstate)
            collection = (game_engine.ECardCollections.Void
                          if tamed_success
                          else card_collection_for_location(row[2]))
            state = (state_after_zone_exit(row[3]) if tamed_success
                     else int(row[3] or 0))
            if tamed_success:
                game.push_card_moved(
                    scid, owner, game_engine.ECardCollections.Void,
                    game_engine.ECardLocations.Top, 0)
            game.push_card_updated(
                scid, owner, collection, ct, template_id=row[0],
                cost=cost, attack=atk, defense=defense,
                state=state,
                int_attrs=(dict(cdef.int_attrs) if cdef else {}),
                gems=gem, nulling=(row[2] == "deck"))
            if tamed_success:
                # Zone exit triggers are part of the normal voiding contract;
                # in particular this keeps capture consistent with other
                # effects that remove a troop from play.
                from .triggers import resolve_triggers
                resolve_triggers(
                    db, handler, game, session, pl_t, ai_t, bstate,
                    "CardExitedZoneEvent", int(target_uid),
                    source_owner_uid=row[1])
            return f"intattr {attr}={value} target={hex(int(target_uid))}"

        if pm.get("property") == "cardcost":
            # Permanent cost reduction while in hand ("Fury of the Mountain
            # God: this gets cost -1 when a troop you control deals damage").
            # Persist on the card so playability and the played cost agree.
            src_uid = (bstate or {}).get("resolving_source_uid")
            cost_target = target_uid or src_uid
            if not cost_target:
                return "cardcost: no target"
            delta = int(pm.get("amount") or 0)
            if delta == 0:
                # Dynamic cost reduction (e.g. Pterobot "cost -1 for each Dwarf
                # and/or Robot you control"): the leaf amount is 0, the real
                # value comes from the ability's m_Variables.  Store the
                # parsed formula on the instance and evaluate it on demand.
                from .cost_mod import formula_from_raw
                raw_row = db.execute(
                    "SELECT raw_json FROM card_abilities_meta "
                    "WHERE ability_guid=?",
                    ((bstate or {}).get("resolving_ability", ""),)).fetchone()
                formula = formula_from_raw(raw_row[0] if raw_row else "")
                if formula:
                    existing = db.execute(
                        "SELECT cost_mod_json FROM game_cards "
                        "WHERE session_id=? AND card_uid=?",
                        (session.session_id, int(cost_target))).fetchone()
                    try:
                        entries = json.loads(existing[0] or "[]") if existing else []
                    except Exception:
                        entries = []
                    # CardCreatedEvent can be replayed during setup/reconnect.
                    # Register one metadata formula per source ability; a
                    # duplicate entry would apply the same dynamic reduction
                    # twice when the card is later displayed in the warzone.
                    if formula not in entries:
                        entries.append(formula)
                    db.execute(
                        "UPDATE game_cards SET cost_mod_json=? "
                        "WHERE session_id=? AND card_uid=?",
                        (json.dumps(entries), session.session_id,
                         int(cost_target)))
                    db.commit()
                    return (f"CardModifier cardcost dynamic "
                            f"zones={formula.get('zones')} "
                            f"x{formula.get('multiplier')} "
                            f"target={hex(int(cost_target))}")
            db.execute(
                "UPDATE game_cards SET card_cost_mod = "
                "COALESCE(card_cost_mod, 0) + ? "
                "WHERE session_id=? AND card_uid=?",
                (delta, session.session_id, int(cost_target)))
            db.commit()
            c_scid = game_engine.SessionCardId(game_engine.UID(int(cost_target)))
            c_trow = db.execute(
                "SELECT template_guid, card_template_id FROM game_cards "
                "WHERE session_id=? AND card_uid=?",
                (session.session_id, int(cost_target))).fetchone()
            c_tpl = c_trow[0] if c_trow else None
            _tpl3, ct3, _n3, cost3, atk3, def3, _g3 = handler._card_full_data(
                game, c_scid, c_tpl, c_trow[1] if c_trow else None)
            card_row = db.execute(
                "SELECT user_id, location FROM game_cards "
                "WHERE session_id=? AND card_uid=?",
                (session.session_id, int(cost_target))).fetchone()
            card_owner = (card_row[0] if card_row
                          else (bstate or {}).get("resolving_owner_id", 0))
            card_location = card_row[1] if card_row else "hand"
            collection = {
                "deck": game_engine.ECardCollections.Deck,
                "hand": game_engine.ECardCollections.Hand,
                "discard": game_engine.ECardCollections.Discard,
                "void": game_engine.ECardCollections.Void,
                "warzone": game_engine.ECardCollections.Warzone,
                "CastSpells": game_engine.ECardCollections.CastSpells,
                "underground": game_engine.ECardCollections.Underground,
                "choosing": game_engine.ECardCollections.Choosing,
            }.get(card_location, game_engine.ECardCollections.Hand)
            game.push_card_updated(
                c_scid, owner_uid(card_owner, pl_t, ai_t, bstate), collection, ct3,
                template_id=_tpl3, cost=cost3, attack=atk3, defense=def3,
                nulling=(card_location == "deck"))
            return f"cost {delta:+} on {hex(int(cost_target))} -> {cost3}"
        if pm.get("property") in ("currentresource", "totalresource",
                                  "threshold", "chargepoints"):
            return _apply_resource_property(game, session, db, handler, pl_t,
                                            ai_t, bstate, pm, target_uid)
        if pm.get("property") == "healhero":
            from .triggers import _apply_health_gain
            amount = _numeric("healhero")
            text = pm.get("text") or ""
            # Escalation: "Gain ESC:4 health." — the amount scales with every
            # escalation spell cast this game (data-driven from the text).
            m_esc = _re.search(r'esc:(\d+)', text, _re.IGNORECASE)
            if m_esc:
                base = int(m_esc.group(1))
                uses = int((bstate or {}).get("player_escalation_uses", 0))
                amount = base * (uses + 1)
                (bstate or {})["player_escalation_uses"] = uses + 1
            if amount <= 0 and not has_dynamic_numeric:
                m_gain = _re.search(r'gain\s+(\d+)\s+health', text.lower())
                amount = int(m_gain.group(1)) if m_gain else 1
            # The source card's real owner is authoritative: for a played
            # spell the card is a game_cards row owned by the caster, so a
            # stale resolving_owner_id (e.g. 0 left by an earlier AI-card
            # trigger) must NOT redirect the heal to the opponent.
            source_owner = None
            src_uid = (bstate or {}).get("resolving_source_uid")
            if src_uid is not None:
                orow = db.execute(
                    "SELECT user_id FROM game_cards "
                    "WHERE session_id=? AND card_uid=?",
                    (session.session_id, int(src_uid))).fetchone()
                if orow:
                    source_owner = orow[0]
            if source_owner is None:
                source_owner = (bstate or {}).get("resolving_owner_id",
                                                  handler.user_profile["id"]
                                                  if handler.user_profile else 0)
            return _apply_health_gain(game, bstate, pl_t, ai_t, amount,
                                      source_owner, db=db, handler=handler,
                                      session=session)
        if pm.get("property") == "attribute":
            temp_attr = pm.get("duration") in (
                "EndOfTurn", "BeginningOfOwnersTurn",
                "AfterCardsReadyOnPlayersTurn")
            attribute_text = pm.get("text") or ""
            if pm.get("attribute_flags"):
                attribute_text = str(pm["attribute_flags"]).replace("|", " ")
            # MultiplePlayers/Warzone attribute effects (Nazhk's
            # CantReadyAutomatically) apply to every opposing troop, even
            # when an older target-template snapshot reports max_target_count=1.
            attribute_targets = [target_uid]
            if ("cantreadyautomatically" in attribute_text.lower()
                    and target_uid is not None):
                source_owner = (bstate or {}).get("resolving_owner_id", 0)
                attribute_targets = [r[0] for r in db.execute(
                    "SELECT card_uid FROM game_cards WHERE session_id=? "
                    "AND user_id<>? AND location='warzone' "
                    "AND card_type LIKE '%Troop%'",
                    (session.session_id, source_owner)).fetchall()]
            attribute_owner = (bstate or {}).get("resolving_owner_id", 0)
            # "AfterCardsReadyOnPlayersTurn" expires at the affected troop's
            # controller's next Prep, not at the source champion's Prep (the
            # Nazhk Webguard power targets opposing troops).
            if pm.get("duration") == "AfterCardsReadyOnPlayersTurn" and target_uid:
                owner_row = db.execute(
                    "SELECT user_id FROM game_cards WHERE session_id=? AND card_uid=?",
                    (session.session_id, int(target_uid))).fetchone()
                if owner_row:
                    attribute_owner = owner_row[0]
            bits = 0
            for attribute_target in attribute_targets:
                bits |= apply_attribute_grant(
                    game, session, db, handler, pl_t, ai_t,
                    attribute_target, attribute_text, temporary=temp_attr,
                    bstate=bstate, duration=pm.get("duration"),
                    source_owner_id=attribute_owner,
                    attribute_flags=pm.get("attribute_flags"))
            return f"attribute grant +{bits:b} target={hex(int(target_uid)) if target_uid else 'none'}"
        if pm.get("property") == "damage":
            if target_uid is None:
                # Deploy "This deals N damage to you" — the 'You' target
                # template means the controller's champion, not the source.
                target_uid = _champion_target_uid(handler, bstate, db, session)
            if target_uid is None:
                # "This deals N damage to each opposing champion" — from the
                # effect's gamedata target template (MultiplePlayers).
                target_uid = _opposing_champion_uid(handler, bstate, db, session)
            if target_uid is None:
                return "damage: no target"
            import re as _re
            from .statics import _leaf_numeric_value
            raw_row = db.execute(
                "SELECT raw_json FROM card_abilities_meta WHERE ability_guid=?",
                ((bstate or {}).get("resolving_ability", ""),)).fetchone()
            raw = raw_row[0] if raw_row else ""
            if not raw:
                raw = json.dumps(ability_record(
                    db, (bstate or {}).get("resolving_ability", "")))
            src_uid = (bstate or {}).get("resolving_source_uid")
            src_owner = (bstate or {}).get("resolving_owner_id", 0)
            esc_handled = False
            # Escalation ("Deal ESC:N damage") takes precedence: N × (1 +
            # escalation cards cast this game) — e.g. Ragefire deals 2, then 4.
            # The gamedata variable for the base N must NOT win here, or the
            # multiplier never applies.
            m_esc = _re.search(r'esc:(\d+)', (pm.get("text") or "").lower())
            if m_esc:
                base = int(m_esc.group(1))
                uses_key = ("ai_escalation_uses"
                            if (bstate or {}).get("resolving_owner_id") == 0
                            else "player_escalation_uses")
                uses = int((bstate or {}).get(uses_key, 0))
                amount = base * (uses + 1)
                # Advance the escalation counter exactly once per resolution:
                # the sibling "Escalation" TAC leaf (Chronic Madness, Ragefire)
                # also fires and must not double-count the same cast.
                if not (bstate or {}).get("_esc_counted_this_resolution"):
                    (bstate or {})[uses_key] = uses + 1
                    (bstate or {})["_esc_counted_this_resolution"] = True
                esc_handled = True
            elif "x damage" in (pm.get("text") or "").lower():
                # "Deal X damage" — X was chosen in the client's X-cost dialog
                # and paid as extra resources when the spell was played.
                amount = int((bstate or {}).get("x_cost", 0) or 0)
                esc_handled = True
            else:
                amount = _leaf_numeric_value(
                    db, session.session_id, bstate, pm, raw, src_owner,
                    int(src_uid) if src_uid else 0, "damage")
            if not esc_handled and amount <= 0:
                m_dmg = _re.search(r'deal\s+(\d+)\s+damage',
                                   (pm.get("text") or "").lower())
                if m_dmg:
                    amount = int(m_dmg.group(1))
            if amount > 0:
                if not esc_handled and (
                        "esc:" in (pm.get("text") or "").lower() or
                        "esc " in (pm.get("text") or "").lower()):
                    bstate["player_escalation_uses"] = int(
                        bstate.get("player_escalation_uses", 0)) + 1
                from .bom import _deal_damage
                return _deal_damage(game, session, db, handler, pl_t, ai_t,
                                    bstate, int(target_uid), amount)
            return "damage: amount 0"
        if pm.get("property") == "counter":
            cname = ""
            counter_guid = pm.get("counter_template_guid")
            if counter_guid:
                try:
                    crow = db.execute(
                        "SELECT name FROM card_counter_templates "
                        "WHERE template_id=?", (counter_guid,)).fetchone()
                    cname = crow[0] if crow else ""
                except Exception:
                    # Minimal unit fixtures predate the extracted counter
                    # catalog.  The typed GUID remains authoritative in live
                    # databases; use the compatibility text only when that
                    # optional lookup table is absent.
                    cname = ""
            cname = cname or counter_name_from_text(pm.get("text")) or "counter"
            amount = int(pm.get("amount") or 0)
            low_text = (pm.get("text") or "").lower()
            operation = str(pm.get("operation") or "").lower()
            is_add_counter = (
                operation == "add" or
                (not operation and "add" in low_text and
                 "counter" in low_text and "remove" not in low_text))
            is_remove_counter = operation in ("remove", "subtract")
            is_set_counter = operation == "set"
            remove_all = bool(pm.get("removeallcounters")) or \
                operation in ("removeall", "clear")
            if amount <= 0 and target_uid and is_add_counter:
                # CountListAttrAbilityVariable values (for example
                # Construction Plans' ExhaustedCards list) are carried in the
                # activation state, while the extracted leaf amount is zero.
                # Resolve that variable instead of falling through to the
                # remove-counters path.
                amount = int(_numeric("counter") or 0)
            if amount > 0 and target_uid and is_remove_counter:
                old_n = card_counters(db, session.session_id, target_uid).get(cname, 0)
                remaining = max(0, old_n - amount)
                remove_card_counters(db, session.session_id, target_uid, cname)
                if remaining:
                    add_card_counter(db, session.session_id, target_uid, cname,
                                     remaining)
                from .effects.counters import push_card_counters
                push_card_counters(game, session, db, handler, pl_t, ai_t,
                                   target_uid, bstate=bstate,
                                   changed_counter=cname, old_value=old_n)
                return f"counter {cname}-{amount} target={hex(int(target_uid))}"
            if is_set_counter and target_uid:
                old_n = card_counters(db, session.session_id, target_uid).get(cname, 0)
                remove_card_counters(db, session.session_id, target_uid, cname)
                if amount > 0:
                    add_card_counter(db, session.session_id, target_uid, cname,
                                     amount)
                from .effects.counters import push_card_counters
                push_card_counters(game, session, db, handler, pl_t, ai_t,
                                   target_uid, bstate=bstate,
                                   changed_counter=cname, old_value=old_n)
                return f"counter {cname} set {amount} target={hex(int(target_uid))}"
            if amount > 0 and target_uid and is_add_counter:
                old_n = card_counters(db, session.session_id, target_uid).get(cname, 0)
                n = add_card_counter(db, session.session_id, target_uid, cname, amount)
                from .effects.counters import push_card_counters
                push_card_counters(game, session, db, handler, pl_t, ai_t,
                                   target_uid, bstate=bstate,
                                   changed_counter=cname, old_value=old_n)
                return f"counter {cname}+{amount} -> {n} target={hex(int(target_uid))}"
            if remove_all or (amount <= 0 and target_uid is None) or (
                    "remove all" in low_text and "all your" in low_text):
                # "remove all <counter> counters from all your <cards> in all
                # zones" (e.g. Incantation of Righteousness): clear the named
                # counter from every matching card the controller owns and
                # stage each card for the ability's transform leaf.  The
                # effect's gamedata condition already gated this leaf.
                from .effects.counters import card_counters as _card_counters
                owner_id = (bstate or {}).get("resolving_owner_id", 0)
                rows = db.execute(
                    "SELECT card_uid, location FROM game_cards "
                    "WHERE session_id=? AND user_id=?",
                    (session.session_id, owner_id)).fetchall()
                cleared = []
                pending = []
                for cu, loc in rows:
                    old_n = _card_counters(db, session.session_id, cu).get(cname, 0)
                    if old_n > 0:
                        remove_card_counters(db, session.session_id, cu, cname)
                        from .effects.counters import push_card_counters
                        push_card_counters(game, session, db, handler, pl_t,
                                           ai_t, cu, changed_counter=cname,
                                           old_value=old_n)
                        cleared.append(int(cu))
                        pending.append((int(cu), loc))
                if pending:
                    bstate["pending_transform_cards"] = pending
                return (f"removed {cname} counters from {len(cleared)} cards "
                        f"(transform {len(pending)})")
            if target_uid:
                old_n = card_counters(db, session.session_id, target_uid).get(cname, 0)
                remove_card_counters(db, session.session_id, target_uid, cname)
                from .effects.counters import push_card_counters
                push_card_counters(game, session, db, handler, pl_t, ai_t,
                                   target_uid, changed_counter=cname,
                                   old_value=old_n)
                return f"counter {cname} cleared on {hex(int(target_uid))}"
            return f"counter {cname}: no target"
        this_turn = pm.get("duration") in ("EndOfTurn", "BeginningOfOwnersTurn",
                                           "AfterCardsReadyOnPlayersTurn")
        if pm.get("property") == "attack":
            atk_d, def_d = _numeric("attack"), 0
        else:
            atk_d, def_d = 0, _numeric("defense")
        if atk_d == 0 and def_d == 0 and "equal to this troop's [def]" in (
                (pm.get("text") or "").lower()):
            # Dynamic stat: "+[ATK] equal to this troop's [DEF]" (Chimera
            # Guard Outrider) — the amount comes from the source card's
            # current defense.
            src_uid = (bstate or {}).get("resolving_source_uid")
            if src_uid is not None:
                srow = db.execute(
                    "SELECT ct.defense, gc.card_defense_mod FROM game_cards gc "
                    "JOIN card_templates ct ON ct.guid = gc.template_guid "
                    "WHERE gc.session_id=? AND gc.card_uid=?",
                    (session.session_id, int(src_uid))).fetchone()
                if srow:
                    src_def = (srow[0] or 0) + (srow[1] or 0)
                    if pm.get("property") == "attack":
                        atk_d, def_d = src_def, 0
                    else:
                        atk_d, def_d = 0, src_def
        if atk_d == 0 and def_d == 0 and "voided troop's" in (
                (pm.get("text") or "").lower()):
            # Champion powers that void a troop then summon a token with its
            # stats ("+[ATK] equal to the voided troop's [ATK] plus 3", e.g.
            # Bun'jitsu): the buff lands on the created token and the amount
            # comes from the voided troop's remembered stats.
            stats = (bstate or {}).get("champion_voided_stats") or {}
            m_plus = _re.search(r'plus\s+(\d+)',
                                (pm.get("text") or "").lower())
            plus = int(m_plus.group(1)) if m_plus else 0
            if pm.get("property") == "attack":
                atk_d = int(stats.get("atk", 0) or 0) + plus
            else:
                def_d = int(stats.get("def", 0) or 0) + plus
            created = (bstate or {}).get("created_token_uids") or []
            if created:
                target_uid = int(created[0])
                (bstate or {})["player_mod_target"] = target_uid
        if target_uid:
            apply_card_stat_mod(game, session, db, handler, pl_t, ai_t,
                                int(target_uid), atk_d, def_d, this_turn=this_turn)
            return f"mod {hex(int(target_uid))} {atk_d:+}/{def_d:+}"
        return f"stat mod: {pm.get('property')} {atk_d if atk_d else def_d:+}"

    name = db.execute(
        "SELECT effect_type FROM ability_effects WHERE effect_guid=? LIMIT 1",
        (effect_guid,)).fetchone()
    eff_name = name[0] if name else ""
    if eff_name != "CardModifierAbilityEffectTemplate":
        # The row is the parent ability, not the effect — skip
        pass

    # Get the game text for the champion ability
    game_text = ""
    ability_guid = bstate.get("resolving_ability", "")
    if ability_guid:
        ca_row = db.execute(
            "SELECT game_text FROM champion_abilities WHERE ability_guid=? LIMIT 1",
            (ability_guid,)).fetchone()
        if ca_row:
            game_text = ca_row[0] or ""
    value = 1
    ability_guid = bstate.get("resolving_ability", "")
    if ability_guid:
        var_row = db.execute(
            "SELECT param FROM ability_effects WHERE ability_guid=? "
            "AND effect_type='RandomizeVariableAbilityEffectTemplate'",
            (ability_guid,)).fetchone()
        if var_row:
            val = _parse_constant(var_row[0])
            if val:
                value = val
        else:
            # No variable — extract value from the effect template name
            m = _re.search(r'(\d+)', game_text)
            if m:
                value = int(m.group(1))

    key = bstate.get("player_health_key", "player_health")

    # Determine modifier type from game text
    lower = game_text.lower()
    if "gain" in lower and "health" in lower:
        return heal_self(game, pl_t, value, bstate, key)
    elif "lose" in lower and "health" in lower or "pay" in lower and "health" in lower:
        return damage_self(game, pl_t, value, bstate, key)
    elif "deal" in lower and "damage" in lower:
        # Deal damage to target — for now treat as self-damage or log
        return f"deal {value} damage"
    elif _re.search(r'[+-]\d+', game_text):
        # Stat modifier — parse target from bstate
        target_uid = (bstate or {}).get("player_mod_target")
        if target_uid:
            atk_d = _parse_stat(game_text, 'ATK')
            def_d = _parse_stat(game_text, 'DEF')
            apply_card_stat_mod(game, session, db, handler, pl_t, ai_t,
                               int(target_uid), atk_d, def_d)
            return f"mod {hex(int(target_uid))} {atk_d:+}/{def_d:+}"
        return f"stat mod: {game_text}"
    else:
        return f"card modifier: {game_text}"


def _parse_constant(param_str):
    """Extract a numeric constant from serialized ability variable data."""
    import struct
    try:
        if not param_str:
            return None
        # Try JSON
        d = json.loads(param_str)
        return d.get("value", d.get("count", None))
    except:
        return None


def _parse_leaf_param(param):
    """Parse an ability_effects.param JSON blob (parent-level child params)."""
    if not param:
        return None
    try:
        d = json.loads(param)
        return d if isinstance(d, dict) else None
    except (ValueError, TypeError):
        return None


def _parse_stat(text, stat):
    """Parse +/-N[STAT] from game text."""
    import re
    m = re.search(r'([+-]\d+)\s*\[' + re.escape(stat) + r'\]', text)
    return int(m.group(1)) if m else 0


def _ability_text(db, bstate):
    """The resolving ability's game text (card or champion ability)."""
    ag = (bstate or {}).get("resolving_ability", "")
    if not ag:
        return ""
    for tbl in ("card_abilities_meta", "champion_abilities"):
        try:
            row = db.execute(
                "SELECT game_text FROM %s WHERE ability_guid=?" % tbl,
                (ag,)).fetchone()
        except Exception:
            continue
        if row:
            return row[0] or ""
    return ""


def _linked_template_guids_from_metadata(db, bstate):
    """Return card ResourceIds linked by the serialized ability metadata.

    Some older ability records do not expose transform choices as a dedicated
    field; they only retain the linked card ResourceIds in the serialized
    record.  Extract the IDs and resolve them through card_templates.  This
    deliberately does not interpret names, percentages, or display wording.
    """
    import re as _re
    ag = (bstate or {}).get("resolving_ability", "")
    row = db.execute(
        "SELECT raw_json FROM card_abilities_meta WHERE ability_guid=?",
        (ag,)).fetchone()
    raw = row[0] if row else ""
    if not raw:
        return []
    links = _re.findall(
        r"data=([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})", raw)
    out = []
    for guid in links:
        guid = guid.lower()
        if db.execute("SELECT 1 FROM card_templates WHERE guid=?", (guid,)).fetchone():
            if guid not in out:
                out.append(guid)
    return out


def _resolve_leaf_target(bstate):
    """The resolved target for a BOM leaf (spell target, ability target,
    trigger target, or the source card itself)."""
    return ((bstate or {}).get("player_spell_target")
            or (bstate or {}).get("player_mod_target")
            or (bstate or {}).get("resolving_target_uid")
            or (bstate or {}).get("resolving_source_uid"))


def _push_card_state(game, session, db, handler, pl_t, ai_t, uid, new_state,
                     bstate=None):
    """Push a CardUpdated in the card's authoritative current collection."""
    from ._shared import card_collection_for_location
    trow = db.execute(
        "SELECT template_guid, user_id, location FROM game_cards WHERE session_id=? AND card_uid=?",
        (session.session_id, int(uid))).fetchone()
    if not trow:
        return
    scid = game_engine.SessionCardId(game_engine.UID(int(uid)))
    _tpl, ct, _n, cost, atk, def_, _g = handler._card_full_data(game, scid, trow[0])
    # In Practice, user_id=0 is the AI and any non-zero id is the human.  PvP
    # has two real non-zero player ids, so use the PvP-aware owner mapping or
    # a CardUpdated for an opponent's damaged troop is rendered under the
    # caster's controller and appears to change sides.  Damage normally
    # supplies bstate; the UID check covers older leaf callers that do not.
    if ((bstate and bstate.get("pvp")) or
            (int(pl_t.uid64) & 0xff) == 244 and
            (int(ai_t.uid64) & 0xff) == 244):
        owner = game_engine.UID.make(244, int(trow[1]))
    else:
        owner = owner_uid(trow[1], pl_t, ai_t, bstate)
    game.push_card_updated(scid, owner, card_collection_for_location(trow[2]), ct,
                           template_id=trow[0], attack=atk, defense=def_,
                           cost=cost, state=new_state,
                           nulling=(trow[2] == "deck"))


def _state_of(db, session, uid):
    row = db.execute(
        "SELECT card_state FROM game_cards WHERE session_id=? AND card_uid=?",
        (session.session_id, int(uid))).fetchone()
    return int(row[0]) if row else 0


def _controller_id_for_target(db, session, handler, bstate, target_uid):
    """Return the DB player id that controls a card or champion target."""
    row = db.execute(
        "SELECT user_id FROM game_cards WHERE session_id=? AND card_uid=?",
        (session.session_id, int(target_uid))).fetchone()
    if row:
        return int(row[0])
    if (bstate or {}).get("pvp"):
        for pid, cuid in ((bstate or {}).get("champ_map") or {}).items():
            try:
                if int(cuid) == int(target_uid):
                    return int(pid)
            except (TypeError, ValueError):
                continue
    for attr, owner in (("_player_champ_scid", handler.user_profile["id"]
                         if handler.user_profile else 0),
                        ("_ai_champ_scid", 0)):
        champ = getattr(handler, attr, None)
        if champ is not None and int(champ.uid.uid64) == int(target_uid):
            return int(owner)
    return None

@leaf_register("SummonTokenTroopAbilityEffectTemplate")
def _leaf_summon(game, session, db, handler, pl_t, ai_t, bstate, effect_guid, param):
    """Compatibility leaf delegating token creation to effects.tokens."""
    return summon_token(game, session, db, handler, pl_t, ai_t, bstate,
                        effect_guid, param)


@leaf_register("ConscriptAbilityEffectTemplate")
def _leaf_conscript(game, session, db, handler, pl_t, ai_t, bstate,
                    effect_guid, param):
    return conscript_cards(game, session, db, handler, pl_t, ai_t, bstate,
                           effect_guid, param)


@leaf_register("LoadPlayerDeckAbilityEffectTemplate")
def _leaf_load_player_deck(game, session, db, handler, pl_t, ai_t, bstate,
                           effect_guid, param):
    return load_player_deck(game, session, db, handler, pl_t, ai_t, bstate,
                            effect_guid, param)


@leaf_register("ActivateTriggeredAbilityEffectTemplate")
def _leaf_activate_triggered(game, session, db, handler, pl_t, ai_t, bstate,
                             effect_guid, param):
    from .triggers import manually_trigger_abilities
    keyword = effect_template_value(db, bstate, effect_guid, "m_Keyword", "")
    target = _resolve_leaf_target(bstate)
    if target is None:
        return "activate triggered: no target"
    result = manually_trigger_abilities(
        db, handler, game, session, pl_t, ai_t, bstate, int(target), keyword)
    return (f"activated {keyword} on {hex(int(target))}: {result}"
            if result else f"activated {keyword}: no matching ability")


def _move_source_card_to_deck(game, session, db, handler, pl_t, ai_t,
                              bstate, src_uid, deck_owner):
    """Move the resolving source card back into its owner's library at a
    uniformly random index, publishing the CardMoved/CardUpdated events.

    Shared tail of a MoveCardToZone whose destination is Deck (an escalation
    spell returning to its library).  ``deck_owner`` is the user id whose deck
    receives the card.
    """
    db.execute(
        "UPDATE game_cards SET location='deck', position=0, card_state=? "
        "WHERE session_id=? AND card_uid=?",
        (state_after_zone_exit(0), session.session_id, int(src_uid)))
    db.commit()
    # Draws leave gaps in the persisted position values.  Choosing a
    # random absolute position therefore biases a returned card toward
    # the top of the deck.  Reinsert against the current ordered deck so
    # every slot is equally likely and only this player's deck is used.
    from db import db_randomly_insert_deck_cards
    db_randomly_insert_deck_cards(
        session.session_id, deck_owner, [int(src_uid)], connection=db)
    pos_row = db.execute(
        "SELECT position FROM game_cards WHERE session_id=? AND card_uid=?",
        (session.session_id, int(src_uid))).fetchone()
    pos = int(pos_row[0]) if pos_row else 0
    scid = game_engine.SessionCardId(game_engine.UID(int(src_uid)))
    trow = db.execute(
        "SELECT template_guid FROM game_cards WHERE session_id=? AND card_uid=?",
        (session.session_id, int(src_uid))).fetchone()
    tpl = trow[0] if trow else None
    _tpl, ct, _n, cost, atk, def_, _g = handler._card_full_data(game, scid, tpl)
    deck_player = owner_uid(deck_owner, pl_t, ai_t, bstate)
    game.push_card_moved(scid, deck_player, game_engine.ECardCollections.Deck,
                         game_engine.ECardLocations.Unknown, 0)
    game.push_card_updated(scid, deck_player, game_engine.ECardCollections.Deck, ct,
                           template_id=tpl, cost=cost, attack=atk,
                           defense=def_, state=0, nulling=True)
    return f"put {hex(src_uid)} into deck (pos {pos})"


@leaf_register("MoveCardToZoneEffectTemplate")
def _leaf_move_card(game, session, db, handler, pl_t, ai_t, bstate, effect_guid, param):
    """Move a card from one zone to another, data-driven from the effect's
    gamedata param (destination/location) — e.g. Eternal Youth's Escalation
    "PutThisIntoYourDeck" (destination Deck), or hand/void/warzone destinations
    for reveal/crypt chains."""
    import json as _json
    src_uid = (bstate or {}).get("resolving_source_uid")
    try:
        p = _json.loads(param or "{}")
    except Exception:
        p = {}
    dest = (p.get("destination") or "").lower()
    # Newer extracted ability effects store the destination on the typed
    # MoveCardToZoneEffectTemplate and leave the compatibility param empty.
    # Prefer that authoritative field whenever the legacy JSON has no zone.
    if not dest:
        typed_dest = effect_template_value(
            db, bstate, effect_guid, "m_DestinationCollection")
        if typed_dest:
            dest = str(typed_dest).rsplit(".", 1)[-1].lower()
    forced_target = None
    # Bane's generated move effect deliberately has no fixed destination:
    # "put the top card of your deck into #DESTINATION_ZONE#" means the zone
    # the Bane currently entered (Hand or Discard). Resolve that contract from
    # the source card's authoritative current zone and controller, then move
    # the top card of that controller's deck through the normal zone path.
    if (dest in ("", "none") and
            (p.get("name") or "") ==
            "PutTheTopCardOfYourDeckIntoDestinationZone"):
        if src_uid is None:
            return "bane move: no source"
        source_row = db.execute(
            "SELECT user_id, location FROM game_cards "
            "WHERE session_id=? AND card_uid=?",
            (session.session_id, int(src_uid))).fetchone()
        if not source_row or source_row[1] not in ("hand", "discard"):
            return "bane move: source is not in hand or discard"
        top_row = db.execute(
            "SELECT card_uid FROM game_cards "
            "WHERE session_id=? AND user_id=? AND location='deck' "
            "ORDER BY position, card_uid LIMIT 1",
            (session.session_id, int(source_row[0]))).fetchone()
        if not top_row:
            return "bane move: deck empty"
        dest = source_row[1]
        forced_target = int(top_row[0])
    # A Deck destination normally means the resolving source card (for
    # effects such as "put this into your deck").  A SourceRevealed target,
    # however, supplies each selected card explicitly and must use the normal
    # target-move path below (Oakhenge's remaining revealed cards).
    resolved_target = _resolve_leaf_target(bstate)
    if (dest == "deck" and resolved_target is not None
            and src_uid is not None and int(resolved_target) != int(src_uid)):
        dest = "deck_target"
    if (p.get("name") or "") == "PutEachCardVoidedByItIntoPlay":
        # "put each card voided by it into play" (Solitary Exile's leave
        # trigger): every card this source voided returns to the warzone.
        return _return_voided_cards(game, session, db, handler, pl_t, ai_t,
                                    bstate, src_uid)
    # The discriminating compiled name lives on the typed effect-template
    # (m_DestinationCollection / effect name), which is only present in the
    # extracted gamedata snapshot.  Fall back to the resolving ability's own
    # authoritative game text — the single source of truth already used by
    # other leaves — to recognize the "return each card it voided into play"
    # contract without hard-coding a card or GUID.
    if 'voided by it into play' in (_ability_text(db, bstate) or '').lower():
        return _return_voided_cards(game, session, db, handler, pl_t, ai_t,
                                    bstate, src_uid)
    if dest == "deck":
        if src_uid is None:
            return "move card: no source"
        source_row = db.execute(
            "SELECT user_id FROM game_cards "
            "WHERE session_id=? AND card_uid=?",
            (session.session_id, int(src_uid))).fetchone()
        if not source_row:
            return f"put {hex(int(src_uid))} into deck: card not found"
        deck_owner = int(source_row[0])
        # MoveCardToZoneEffectTemplate uses -2 for
        # ControlGivenToTargetIndex when the previous target controls the
        # destination card.  Reginald is the important case: after damaging an
        # opposing champion, its deck destination is that champion's deck,
        # not the source card's original deck.  The first effect in the child
        # ability stores the previous target, so resolve the owner from that
        # stored target without referring to a card name.
        move_name = str(p.get("name") or "")
        if p.get("control_given_to_target_index") == -2 or (
                "PreviousTargetControls" in move_name):
            current_ability = (bstate or {}).get("resolving_ability", "")
            stored = ((bstate or {}).get("stored_targets", {})
                      .get(current_ability) or [])
            previous_target = stored[-1] if stored else None
            if previous_target is not None:
                target_owner = _controller_id_for_target(
                    db, session, handler, bstate, previous_target)
                if target_owner is not None:
                    deck_owner = int(target_owner)
                    db.execute(
                        "UPDATE game_cards SET user_id=? "
                        "WHERE session_id=? AND card_uid=?",
                        (deck_owner, session.session_id, int(src_uid)))
                    db.commit()
        return _move_source_card_to_deck(
            game, session, db, handler, pl_t, ai_t, bstate, int(src_uid),
            deck_owner)
    # Other destinations: move the resolved target (or the source card).
    target = (forced_target if forced_target is not None
              else _resolve_leaf_target(bstate))
    if target is None:
        return "move card: no target/source"
    # A MoveCardToZone whose destination could not be resolved from the effect
    # metadata (param / typed m_DestinationCollection, e.g. a deck-search
    # effect whose authoritative zone lives only in the extracted gamedata
    # snapshot) but whose resolved target currently resides in its controller's
    # deck is a deck-search effect: "search your deck for X" moves the chosen
    # card to its controller's hand.  The destination is established here from
    # the target's current zone (deck), not from any card name or GUID.
    if not dest and target is not None:
        row = db.execute(
            "SELECT user_id, location FROM game_cards "
            "WHERE session_id=? AND card_uid=?",
            (session.session_id, int(target))).fetchone()
        if row is not None and row[0] and row[1] == "deck":
            from .effects.search import move_deck_card_to_hand
            return move_deck_card_to_hand(
                game, session, db, handler, pl_t, ai_t,
                int(target), int(row[0]), bstate)
        # An Escalation spell's return-to-library is delivered as a
        # self-target MoveCardToZone whose destination (Deck) again lives only
        # in the extracted gamedata snapshot.  It resolves the resolving source
        # card against its own current zone (hand / in play) and is recognized
        # by the ability's authoritative Escalation text — never by card name.
        elif (int(target) == int(src_uid)
              and 'escalation' in (_ability_text(db, bstate) or '').lower()):
            sr = db.execute(
                "SELECT user_id FROM game_cards "
                "WHERE session_id=? AND card_uid=?",
                (session.session_id, int(src_uid))).fetchone()
            if sr is not None and sr[0] is not None:
                return _move_source_card_to_deck(
                    game, session, db, handler, pl_t, ai_t, bstate,
                    int(src_uid), int(sr[0]))
    zone = {"hand": ("hand", game_engine.ECardCollections.Hand),
            "deck_target": ("deck", game_engine.ECardCollections.Deck),
            "warzone": ("warzone", game_engine.ECardCollections.Warzone),
            "discard": ("discard", game_engine.ECardCollections.Discard),
            "void": ("void", game_engine.ECardCollections.Void)}.get(dest)
    if not zone:
        return "move card between zones"
    loc, coll = zone
    old_row = db.execute(
        "SELECT location, card_state FROM game_cards WHERE session_id=? AND card_uid=?",
        (session.session_id, int(target))).fetchone()
    old_loc = old_row[0] if old_row else None
    old_state = int(old_row[1] or 0) if old_row else 0
    if loc == "warzone":
        db.execute(
            "UPDATE game_cards SET location=?, position=0, "
            "card_state = card_state & ~? "
            "WHERE session_id=? AND card_uid=?",
            (loc, game_engine.ECardStates.Dead, session.session_id, int(target)))
    else:
        db.execute(
            "UPDATE game_cards SET location=?, position=?, card_state=? "
            "WHERE session_id=? AND card_uid=?",
            (loc, 100 if loc == "hand" else 0, state_after_zone_exit(0),
             session.session_id, int(target)))
    db.commit()
    scid = game_engine.SessionCardId(game_engine.UID(int(target)))
    trow = db.execute(
        "SELECT template_guid FROM game_cards WHERE session_id=? AND card_uid=?",
        (session.session_id, int(target))).fetchone()
    tpl = trow[0] if trow else None
    _tpl, ct, _n, cost, atk, def_, _g = handler._card_full_data(game, scid, tpl)
    # The selected/revealed card is now a normal hand card.  Re-materialize
    # its full definition from the authoritative template_guid and publish the
    # hand transition as a draw so the client's CardRepresentation cannot
    # retain the source Oakhenge instance's art/definition.
    owner_row = db.execute(
        "SELECT user_id FROM game_cards WHERE session_id=? AND card_uid=?",
        (session.session_id, int(target))).fetchone()
    owner = owner_uid(owner_row[0] if owner_row else 0, pl_t, ai_t, bstate)
    game.push_card_moved(scid, owner, coll,
                         game_engine.ECardLocations.Unknown if loc == "deck"
                         else game_engine.ECardLocations.Top,
                         1 if loc == "hand" else 0)
    if loc == "hand" and old_loc == "deck":
        game.push_card_drawn(scid, owner, 1)
    current_state = db.execute(
        "SELECT card_state FROM game_cards WHERE session_id=? AND card_uid=?",
        (session.session_id, int(target))).fetchone()
    game.push_card_updated(
        scid, owner, coll, ct, template_id=tpl, cost=cost,
        attack=atk, defense=def_, state=int(current_state[0] or 0)
        if current_state else 0, nulling=(loc == "deck"))
    # Zone entry is an event in its own right.  Draw helpers emit this for
    # normal draws, while generic BOM moves must emit it here so Hand|Discard
    # triggers (for example a Reginald buried into its controller's discard)
    # fire regardless of which effect moved the card.
    if loc in ("hand", "discard"):
        owner_id = db.execute(
            "SELECT user_id FROM game_cards WHERE session_id=? AND card_uid=?",
            (session.session_id, int(target))).fetchone()
        from .triggers import resolve_triggers
        resolve_triggers(
            db, handler, game, session, pl_t, ai_t, bstate,
            "CardEnteredZoneEvent", int(target),
            source_owner_uid=(int(owner_id[0]) if owner_id else 0),
            event_source_collection=old_loc,
            event_destination_collection=loc,
            event_previous_state=old_state)
    if loc == "deck" and dest == "deck_target":
        # A revealed-card choice such as Oakhenge returns the unchosen cards
        # to the deck.  Merely changing their location leaves all of them at
        # position zero, which makes the next draw deterministic and differs
        # from the client's shuffle-into-deck behavior.  Reinsert every
        # revealed card still in the deck into random slots while preserving
        # the rest of the deck order.
        revealed = [int(uid) for uid in
                    (bstate or {}).get("revealed_cards", [])]
        owner_row = db.execute(
            "SELECT user_id FROM game_cards WHERE session_id=? AND card_uid=?",
            (session.session_id, int(target))).fetchone()
        if revealed and owner_row:
            from db import db_randomly_insert_deck_cards
            db_randomly_insert_deck_cards(
                session.session_id, int(owner_row[0]), revealed,
                connection=db)
    return f"moved {hex(int(target))} to {'deck' if dest == 'deck_target' else dest}"


def _return_voided_cards(game, session, db, handler, pl_t, ai_t, bstate,
                         src_uid):
    """'put each card voided by it into play' — move every card the source
    voided (tracked in bstate.voided_by) back to the warzone.  Data-driven
    from the leaf name, mirroring the client's zone-exit resolution."""
    if src_uid is None:
        return "return voided: no source"
    vby = (bstate or {}).get("voided_by") or {}
    uids = list(vby.get(str(int(src_uid)), []))
    returned = 0
    for target_uid in uids:
        row = db.execute(
            "SELECT user_id FROM game_cards WHERE session_id=? AND card_uid=?",
            (session.session_id, int(target_uid))).fetchone()
        if not row:
            continue
        owner = pl_t if row[0] != 0 else ai_t
        db.execute(
            "UPDATE game_cards SET location='warzone', position=0, "
            "card_state = (card_state & ~?) | ? "
            "WHERE session_id=? AND card_uid=?",
            (game_engine.ECardStates.StartedATurnOnYourSide |
             game_engine.ECardStates.Dead,
             game_engine.ECardStates.CameOutThisTurn,
             session.session_id, int(target_uid)))
        db.commit()
        scid = game_engine.SessionCardId(game_engine.UID(int(target_uid)))
        trow = db.execute(
            "SELECT template_guid FROM game_cards WHERE session_id=? AND card_uid=?",
            (session.session_id, int(target_uid))).fetchone()
        tpl_guid = trow[0] if trow else None
        _tpl, ct, _n, _c, atk, def_, _g = handler._card_full_data(
            game, scid, tpl_guid)
        game.push_card_moved(scid, owner, game_engine.ECardCollections.Warzone,
                             game_engine.ECardLocations.Top, 0)
        game.push_card_updated(scid, owner, game_engine.ECardCollections.Warzone,
                               ct, template_id=tpl_guid, attack=atk,
                               defense=def_)
        # The returned card entered play — fire its enters-play triggers.
        from .triggers import resolve_enters_play_triggers
        resolve_enters_play_triggers(db, handler, game, session, pl_t, ai_t,
                                     bstate, int(target_uid), row[0], 0)
        returned += 1
    if uids:
        vby[str(int(src_uid))] = []
    return f"returned {returned} voided cards"

@leaf_register("BuryCardAbilityEffectTemplate")
def _leaf_bury(game, session, db, handler, pl_t, ai_t, bstate, effect_guid, param):
    """Bury (mill) the top N cards of a deck (Chronic Madness: "Bury the top
    ESC:4 cards of target champion's deck")."""
    count = 1
    try:
        if param:
            d = json.loads(param)
            count = d.get("count", 1)
    except:
        pass
    ability_guid = (bstate or {}).get("resolving_ability", "")
    if ability_guid:
        raw_row = db.execute(
            "SELECT raw_json FROM card_abilities_meta WHERE ability_guid=?",
            (ability_guid,)).fetchone()
        if raw_row and raw_row[0]:
            from .statics import _variable_value
            src = (bstate or {}).get("resolving_source_uid")
            v = _variable_value(
                db, session.session_id, bstate, raw_row[0], "amount",
                (bstate or {}).get("resolving_owner_id", 0),
                int(src) if src else 0)
            if v is not None:
                count = int(v)
    # The target champion's deck (or the AI deck when no champion target).
    deck_owner = None
    target = (bstate or {}).get("player_spell_target")
    if target is not None:
        deck_owner = _deck_owner_for_target(db, handler, session, bstate, target)
    if deck_owner is None:
        deck_owner = 0
    discard_owner = owner_uid(deck_owner, pl_t, ai_t, bstate)
    total = 0
    for _ in range(max(0, count)):
        row = db.execute(
            "SELECT id, card_uid, template_guid FROM game_cards "
            "WHERE session_id=? AND user_id=? AND location='deck' "
            "ORDER BY position LIMIT 1",
            (session.session_id, deck_owner)).fetchone()
        if not row:
            break
        scid = game_engine.SessionCardId(game_engine.UID(row[1]))
        from db import db_discard_card
        db_discard_card(session.session_id, row[1], connection=db)
        # Face-up in the discard: a bare CardMoved leaves the client's crypt
        # empty — push a full CardUpdated so the buried card renders (the
        # DB move alone is invisible, which read as "not burying").
        _tpl2, ct2, _n2, _c2, atk2, def2, _g2 = handler._card_full_data(
            game, scid, row[2])
        game.push_card_updated(scid, discard_owner,
                               game_engine.ECardCollections.Discard, ct2,
                               template_id=row[2], attack=atk2, defense=def2)
        game.push_card_moved(scid, discard_owner, game_engine.ECardCollections.Discard,
                             game_engine.ECardLocations.Top, 1)
        total += 1
        # The buried card entered the crypt — "when a card enters an opposing
        # crypt" triggers (Incantation of Fear) fire here.
        from .triggers import resolve_triggers
        resolve_triggers(db, handler, game, session, pl_t, ai_t, bstate,
                         "CardEnteredZoneEvent", int(row[1]),
                         source_owner_uid=deck_owner,
                         event_source_collection="deck",
                         event_destination_collection="discard",
                         event_previous_state=0)
    return f"bury {total} cards"

@leaf_register("CounterSpellAbilityEffectTemplate")
def _leaf_counter_spell(game, session, db, handler, pl_t, ai_t, bstate,
                        effect_guid, param):
    """CounterSpell leaf — interrupt a target card on the chain (Countermagic
    "Interrupt target card").  The resolution engine sets
    bstate['player_spell_target'] to the chosen CastSpells card; the shared
    helper moves it to the graveyard so its BOM never resolves."""
    from .triggers import _resolve_counter_spell
    return _resolve_counter_spell(db, handler, game, session, pl_t, ai_t,
                                  bstate, effect_guid, param, "")

@leaf_register("VoidCardAbilityEffectTemplate")
def _leaf_void(game, session, db, handler, pl_t, ai_t, bstate, effect_guid, param):
    """Exile (void) a card: move the target to the Void and remember it under
    the resolving source card so a "when this leaves play, put each card voided
    by it into play" ability can return it."""
    import re as _re
    # The trigger's chosen target (Solitary Exile's Deploy "Void another target
    # card") is the authoritative target — it must win over the generic
    # player_spell_target / player_mod_target fields, which can hold STALE
    # values from an earlier spell or champion-power activation.
    target_uid = (bstate or {}).get("resolving_target_uid")
    if target_uid is None:
        target_uid = ((bstate or {}).get("player_spell_target")
                      or (bstate or {}).get("player_mod_target"))
    if target_uid is None:
        return "void: no target"
    row = db.execute(
        "SELECT user_id FROM game_cards WHERE session_id=? AND card_uid=?",
        (session.session_id, int(target_uid))).fetchone()
    if not row:
        return "void: target not found"
    owner = pl_t if row[0] != 0 else ai_t
    db.execute(
        "UPDATE game_cards SET location='void', position=0 "
        "WHERE session_id=? AND card_uid=?",
        (session.session_id, int(target_uid)))
    db.commit()
    scid = game_engine.SessionCardId(game_engine.UID(int(target_uid)))
    tpl_row = db.execute(
        "SELECT template_guid FROM game_cards WHERE session_id=? AND card_uid=?",
        (session.session_id, int(target_uid))).fetchone()
    tpl_guid = tpl_row[0] if tpl_row else None
    _tpl, ct, _n, _c, atk, def_, _g = handler._card_full_data(game, scid, tpl_guid)
    game.push_card_moved(scid, owner, game_engine.ECardCollections.Void,
                         game_engine.ECardLocations.Top, 0)
    game.push_card_updated(scid, owner, game_engine.ECardCollections.Void, ct,
                           template_id=tpl_guid, attack=atk, defense=def_)
    # The card LEFT its zone — fire CardExitedZoneEvent so "when this leaves
    # play" triggers resolve (e.g. a Solitary Exile that was voided returns the
    # cards it exiled — the client fires the same event for zone exits).
    from .triggers import resolve_triggers
    resolve_triggers(db, handler, game, session, pl_t, ai_t, bstate,
                     "CardExitedZoneEvent", int(target_uid),
                     source_owner_uid=row[0])
    src_uid = (bstate or {}).get("resolving_source_uid")
    if src_uid is not None:
        bstate.setdefault("voided_by", {}).setdefault(str(int(src_uid)), []).append(int(target_uid))
        # Push the source card's CardUpdated with RelatedCards so the client
        # draws the voided-card relationship (examine panel / border link).
        srow = db.execute(
            "SELECT template_guid, user_id, location FROM game_cards "
            "WHERE session_id=? AND card_uid=?",
            (session.session_id, int(src_uid))).fetchone()
        if srow and srow[2] == "warzone":
            sscid = game_engine.SessionCardId(game_engine.UID(int(src_uid)))
            _tpl2, ct2, _n2, _c2, atk2, def2, _g2 = handler._card_full_data(
                game, sscid, srow[0])
            sowner = pl_t if (srow[1] or 0) != 0 else ai_t
            game.push_card_updated(sscid, sowner, game_engine.ECardCollections.Warzone,
                                   ct2, template_id=srow[0], attack=atk2,
                                   defense=def2,
                                   related_cards=[game_engine.SessionCardId(
                                       game_engine.UID(int(target_uid)))])
    return f"voided {hex(int(target_uid))}"

@leaf_register("UntapCardAbilityEffectTemplate")
def _leaf_untap(game, session, db, handler, pl_t, ai_t, bstate, effect_guid, param):
    """Ready (untap) the target troop, or every friendly warzone troop for
    "ready each ... you control" effects."""
    text = _ability_text(db, bstate)
    target = _resolve_leaf_target(bstate)
    if target is not None:
        uids = [int(target)]
    elif "each" in (text or "").lower():
        owner = int((bstate or {}).get("resolving_owner_id", 0))
        uids = [r[0] for r in db.execute(
            "SELECT card_uid FROM game_cards WHERE session_id=? "
            "AND location='warzone' AND card_type LIKE '%Troop%' AND user_id=?",
            (session.session_id, owner)).fetchall()]
    else:
        return "untap: no target"
    for u in uids:
        db.execute(
            "UPDATE game_cards SET card_state = card_state & ~? "
            "WHERE session_id=? AND card_uid=?",
            (game_engine.ECardStates.Tapped, session.session_id, u))
        row = db.execute(
            "SELECT card_state FROM game_cards WHERE session_id=? AND card_uid=?",
            (session.session_id, u)).fetchone()
        _push_card_state(game, session, db, handler, pl_t, ai_t, u,
                         int(row[0]) if row else 0)
    db.commit()
    return f"readied {len(uids)}"

@leaf_register("TapCardAbilityEffectTemplate")
def _leaf_tap(game, session, db, handler, pl_t, ai_t, bstate, effect_guid, param):
    """Exhaust (tap) the target troop, or every opposing warzone troop for
    "exhaust each opposing troop" effects."""
    text = _ability_text(db, bstate)
    target = _resolve_leaf_target(bstate)
    if target is not None:
        uids = [int(target)]
    elif "each" in (text or "").lower():
        owner = int((bstate or {}).get("resolving_owner_id", 0))
        uids = [r[0] for r in db.execute(
            "SELECT card_uid FROM game_cards WHERE session_id=? "
            "AND location='warzone' AND card_type LIKE '%Troop%' AND user_id!=?",
            (session.session_id, owner)).fetchall()]
    else:
        return "tap: no target"
    for u in uids:
        db.execute(
            "UPDATE game_cards SET card_state = card_state | ? "
            "WHERE session_id=? AND card_uid=?",
            (game_engine.ECardStates.Tapped, session.session_id, u))
        row = db.execute(
            "SELECT card_state FROM game_cards WHERE session_id=? AND card_uid=?",
            (session.session_id, u)).fetchone()
        _push_card_state(game, session, db, handler, pl_t, ai_t, u,
                         int(row[0]) if row else game_engine.ECardStates.Tapped)
        owner_row = db.execute(
            "SELECT user_id FROM game_cards WHERE session_id=? AND card_uid=?",
            (session.session_id, int(u))).fetchone()
        from .triggers import resolve_triggers
        resolve_triggers(
            db, handler, game, session, pl_t, ai_t, bstate,
            "CardTappedEvent", int(u), owner_row[0] if owner_row else 0)
    db.commit()
    return f"tapped {len(uids)}"

@leaf_register("DestroyCardAbilityEffectTemplate")
def _leaf_destroy(game, session, db, handler, pl_t, ai_t, bstate, effect_guid, param):
    """Destroy the resolved target (or the source card) — kill to graveyard,
    firing Deathcry, via the shared kill_troop path."""
    from .kill_troop import kill_troop
    target = _resolve_leaf_target(bstate)
    if target is None:
        return "destroy: no target"
    # Champions are session targets, not game_cards rows.  "Destroy you"
    # effects (such as a card entering its controller's hand after an
    # opponent's effect) therefore need to end the battle directly rather
    # than falling through the troop graveyard helper.
    card_row = db.execute(
        "SELECT 1 FROM game_cards WHERE session_id=? AND card_uid=?",
        (session.session_id, int(target))).fetchone()
    if not card_row:
        target_owner = _controller_id_for_target(
            db, session, handler, bstate, int(target))
        if target_owner is not None:
            if (bstate or {}).get("pvp"):
                from services.tournament_game import _pvp_end_game
                pids = [int(pid) for pid in (bstate.get("pids") or [])]
                winner = next((pid for pid in pids if pid != target_owner), None)
                if winner is not None:
                    _pvp_end_game(session, bstate, winner, int(target_owner),
                                  "champion destroyed by card effect")
                    return f"destroyed champion {hex(int(target))}"
            else:
                import commands as _cmd
                winner_uid = pl_t if int(target_owner) == 0 else ai_t
                loser_uid = ai_t if int(target_owner) == 0 else pl_t
                _cmd.push_battle_game_end(
                    handler=handler, session=session,
                    winners=[winner_uid], losers=[loser_uid])
                if hasattr(handler, "_campaign_gameend"):
                    handler._campaign_gameend(session, won=(int(target_owner) == 0))
                return f"destroyed champion {hex(int(target))}"
    kill_troop(game, session, db, handler, pl_t, ai_t, int(target),
               bstate, cause="effect")
    return f"destroyed {hex(int(target))}"

@leaf_register("RevealCardsAbilityEffectTemplate")
def _leaf_reveal(game, session, db, handler, pl_t, ai_t, bstate, effect_guid, param):
    """Reveal cards described by the effect's target-template metadata."""
    count = 1
    target_kind = ""
    reveal_collection = game_engine.ECardCollections.Deck
    ability_guid = (bstate or {}).get("resolving_ability", "")
    try:
        erow = db.execute(
            "SELECT target_index FROM ability_effects "
            "WHERE ability_guid=? AND effect_guid=?",
            (ability_guid, effect_guid)).fetchone()
    except Exception:
        # Minimal focused leaf fixtures predate the normalized BOM table.
        # Preserve their text-based count fallback while production uses the
        # extracted target metadata above.
        erow = None
    if not erow:
        import re as _re
        text = (_ability_text(db, bstate) or "").lower()
        words = {"one": 1, "two": 2, "three": 3, "four": 4,
                 "five": 5, "six": 6, "seven": 7, "eight": 8,
                 "nine": 9, "ten": 10}
        match = _re.search(
            r"(?:look at|reveal)\s+(one|two|three|four|five|six|seven|"
            r"eight|nine|ten|\d+)\s+.*?card", text)
        if match:
            count = (int(match.group(1)) if match.group(1).isdigit()
                     else words.get(match.group(1), 1))
    if erow:
        trow = db.execute(
            "SELECT target_template_ids FROM card_abilities_meta "
            "WHERE ability_guid=?", (ability_guid,)).fetchone()
        try:
            tids = json.loads(trow[0]) if trow and trow[0] else []
            tid = tids[int(erow[0])] if 0 <= int(erow[0]) < len(tids) else None
            frow = db.execute(
                "SELECT filter_json, target_kind FROM target_templates "
                "WHERE template_id=?",
                (str(tid),)).fetchone() if tid else None
            filt = json.loads(frow[0]) if frow and frow[0] else {}
            target_kind = (frow[1] or "") if frow else ""
            top = next((f for f in filt.get("m_TargetFilters", [])
                        if str(f.get("_t", "")).split(".")[-1]
                        == "TopNOfDeck"), None)
            if top:
                count = max(0, int(top.get("m_Amount", 1) or 1))
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    owner = int((bstate or {}).get("resolving_owner_id", 0))
    # The reveal effect's target template specifies the source zone.  Do not
    # assume Deck: Shadowgrove Witch's child ability targets a random card in
    # the opposing champion's Hand, while other reveal effects target Deck.
    reveal_zone = "deck"
    try:
        trow = db.execute(
            "SELECT target_template_ids FROM card_abilities_meta "
            "WHERE ability_guid=?", (ability_guid,)).fetchone()
        tids = json.loads(trow[0]) if trow and trow[0] else []
        tid = tids[int(erow[0])] if erow and 0 <= int(erow[0]) < len(tids) else None
        frow = db.execute(
            "SELECT filter_json FROM target_templates WHERE template_id=?",
            (str(tid),)).fetchone() if tid else None
        filt = json.loads(frow[0]) if frow and frow[0] else {}
        stack = [filt]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                typ = str(node.get("_t", "")).rsplit(".", 1)[-1]
                if typ == "InZone" and node.get("m_Collection"):
                    reveal_zone = str(node["m_Collection"]).lower()
                    break
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    # The reveal event must carry the same collection as the metadata-selected
    # source zone.  Leaving the default Deck collection makes a hand reveal
    # render in the client's deck coverflow even when the correct hand card
    # was selected server-side.
    if target_kind != "AbilitySourceCardTargetTemplate":
        from ._shared import card_collection_for_location
        reveal_collection = card_collection_for_location(reveal_zone)
    if target_kind == "AbilitySourceCardTargetTemplate":
        # A source-card reveal (Argus: "reveal Argus from your hand") is not
        # a deck reveal.  The resolution engine has already resolved the
        # source target from the metadata target template and stores it as
        # the current target UID.  Use that card directly so the following
        # SourceRevealed target applies the modifier to the same instance.
        source_target = _resolve_leaf_target(bstate)
        row = None
        if source_target is not None:
            row = db.execute(
                "SELECT card_uid, position, template_guid, user_id, card_state, "
                "location FROM game_cards WHERE session_id=? AND card_uid=? "
                "AND user_id=?",
                (session.session_id, int(source_target), owner)).fetchone()
        rows = [row[:5]] if row else []
        if row:
            from ._shared import card_collection_for_location
            reveal_collection = card_collection_for_location(row[5])
    else:
        rows = db.execute(
            "SELECT card_uid, position, template_guid, user_id, card_state "
            "FROM game_cards WHERE session_id=? "
            "AND user_id=? AND location=? ORDER BY position LIMIT ?",
            (session.session_id, owner, reveal_zone, count)).fetchall()
        if reveal_zone == "hand" and rows:
            rows = [random.choice(rows)]
    uids = [int(r[0]) for r in rows]
    bstate["revealed_cards"] = uids
    if uids:
        # CardsRevealed only identifies the instances.  The client resolves
        # those ids through its card cache, so publish the current full card
        # definition first; otherwise a revealed card remains a face-down /
        # partial deck representation in the coverflow UI.  RevealCards also
        # carries an explicit recipient policy in gamedata.  A Self/You
        # "look at" must be delivered only to its controller in PvP; a shared
        # packet would disclose the cards to the opponent even though the
        # event's player_id names the controller.
        from ._shared import owner_uid
        effect_param = param or ""
        reveal_targets = "Everyone"
        try:
            meta = json.loads(effect_param) if effect_param else {}
            if isinstance(meta, dict):
                reveal_targets = str(
                    meta.get("player_reveal_targets") or "Everyone")
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        private = False
        if (bstate or {}).get("pvp") and reveal_targets.lower() in (
                "self", "you", "controller"):
            private_sender = getattr(handler, "_push_private_revealed_cards",
                                     None)
            if callable(private_sender):
                private = bool(private_sender(
                    session, bstate, owner, rows, pl_t, ai_t))
        if not private:
            for row in rows:
                scid = game_engine.SessionCardId(game_engine.UID(int(row[0])))
                card_owner = owner_uid(row[3], pl_t, ai_t, bstate)
                _tpl, ct, _name, cost, atk, defense, _gem = \
                    handler._card_full_data(game, scid, row[2])
                game.push_card_updated(
                    scid, card_owner, reveal_collection, ct,
                    state=int(row[4] or 0), template_id=row[2], cost=cost,
                    attack=atk, defense=defense, nulling=False)
            ev = game_engine.CardsRevealedSessionEventArgs()
            ev.player_id = owner_uid(owner, pl_t, ai_t, bstate)
            ev.session_card_ids = [game_engine.SessionCardId(game_engine.UID(u))
                                   for u in uids]
            ev.collections = [reveal_collection] * len(uids)
            ev.owning_players = [ev.player_id] * len(uids)
            ev.positions = [int(r[1] or 0) for r in rows]
            game._push(ev)
    return f"revealed {len(uids)}"

@leaf_register("StoreTargetsAbilityEffectTemplate")
def _leaf_store_targets(game, session, db, handler, pl_t, ai_t, bstate,
                        effect_guid, param):
    """Remember the resolved target so later effects in the same ability can
    reference it (e.g. "Target that troop. It gets +2/+2")."""
    target = _resolve_leaf_target(bstate)
    if target is None:
        return "store targets: none"
    ag = (bstate or {}).get("resolving_ability", "")
    stored = (bstate or {}).setdefault("stored_targets", {})
    stored.setdefault(ag, []).append(int(target))
    return f"stored {hex(int(target))}"


@leaf_register("StoreListAttrAbilityEffectTemplate")
def _leaf_store_list_attr(game, session, db, handler, pl_t, ai_t, bstate,
                          effect_guid, param):
    """Persist one typed TAC list entry for later effects in this ability.

    The Python battle state is the server-side equivalent of the client's
    AbilityInstance TAC.  Keeping the list keyed by ability and honoring Set
    is enough for shard selectors and list-based EffectFields without
    leaking transient choices into the card instance.
    """
    template = effect_template(effect_guid) or {}
    list_name = str(template.get("m_ListAttrName") or "")
    attr_name = str(template.get("m_IntAttrName") or "")
    if not list_name:
        return "store list: no list name"
    ag = (bstate or {}).get("resolving_ability", "")
    lists = (bstate or {}).setdefault("list_attrs", {}).setdefault(ag, {})
    if template.get("m_Set"):
        lists[list_name] = []
    lists.setdefault(list_name, []).append({
        "name": attr_name,
        "value": int(template.get("m_IntAttrValue") or 0),
        "until_end_of_turn": bool(template.get("m_OnlyUntilEndOfTurn")),
    })
    return f"stored {attr_name} in {list_name}"


@leaf_register("RememberKeywordPowersEffectTemplate")
def _leaf_remember_keyword_powers(game, session, db, handler, pl_t, ai_t,
                                  bstate, effect_guid, param):
    """Remember matching current ability GUIDs for a later GrantAbility leaf."""
    target = _resolve_leaf_target(bstate)
    if target is None:
        return "remember powers: no target"
    template = effect_template(effect_guid) or {}
    keyword = template.get("m_Keyword") or ""
    all_powers = bool(template.get("m_AllPowers"))
    from .triggers import ability_matches_keyword, _card_ability_guids
    remembered = (bstate or {}).setdefault("remembered_powers", {}).setdefault(
        (bstate or {}).get("resolving_ability", ""), [])
    for ag in _card_ability_guids(db, session, int(target)):
        if all_powers or ability_matches_keyword(db, ag, keyword):
            if ag not in remembered:
                remembered.append(ag)
    return f"remembered {len(remembered)} {keyword} power(s)"


def _card_atk(db, session, uid, bstate=None):
    """Return the current combat ATK, including instance/static modifiers."""
    from .statics import effective_stats
    atk, _def, _attrs, _flags, _rage = effective_stats(
        db, session.session_id, bstate or {}, int(uid))
    return atk


def _deal_damage(game, session, db, handler, pl_t, ai_t, bstate, uid, amount):
    """Compatibility entry point for the focused damage effect module."""
    return deal_damage(game, session, db, handler, pl_t, ai_t, bstate, uid,
                       amount)




@leaf_register("RevertPermanentModificationsAbilityEffectTemplate")
def _leaf_revert_mods(game, session, db, handler, pl_t, ai_t, bstate,
                      effect_guid, param):
    """Revert the target's permanent modifications: attack/defense/cost mods
    and permanent atk/def buffs (counters are kept)."""
    import json as _json
    target = _resolve_leaf_target(bstate)
    if target is None:
        return "revert: no target"
    prow = db.execute(
        "SELECT permanent_buffs FROM game_cards WHERE session_id=? AND card_uid=?",
        (session.session_id, int(target))).fetchone()
    try:
        data = _json.loads((prow[0] if prow else "{}") or "{}")
    except Exception:
        data = {}
    if "atk" in data:
        data["atk"] = 0
    if "def" in data:
        data["def"] = 0
    db.execute(
        "UPDATE game_cards SET card_attack_mod=0, card_defense_mod=0, "
        "card_cost_mod=0, permanent_buffs=? WHERE session_id=? AND card_uid=?",
        (_json.dumps(data), session.session_id, int(target)))
    db.commit()
    _push_card_state(game, session, db, handler, pl_t, ai_t, int(target),
                     _state_of(db, session, int(target)))
    return f"reverted {hex(int(target))}"


@leaf_register("Battle2CardsAbilityEffectTemplate")
def _leaf_battle(game, session, db, handler, pl_t, ai_t, bstate, effect_guid, param):
    """Battle2Cards: the source (and/or a remembered target) deals its ATK to
    the resolved target.  "battles" -> both deal; "deals damage equal to its
    [ATK]" -> only the source deals."""
    text = _ability_text(db, bstate) or ""
    low = text.lower()
    source = (bstate or {}).get("resolving_source_uid")
    target = _resolve_leaf_target(bstate)
    stored = ((bstate or {}).get("stored_targets", {})
              .get((bstate or {}).get("resolving_ability", "")) or [])
    uids = []
    if source is not None:
        uids.append(int(source))
    if target is not None and int(target) not in uids:
        uids.append(int(target))
    for s in stored:
        if int(s) not in uids:
            uids.append(int(s))
    if len(uids) < 2:
        return "battle: need two cards"
    a, d = uids[0], uids[-1]
    logs = []
    # Battle's extracted form for Tharg's Warrior talent is not the generic
    # "both cards battle" operation.  It says the remembered troop deals its
    # ATK to *you*.  The source is the champion, so the normal source->target
    # direction would calculate a champion's zero ATK and never damage the
    # player.  Resolve the troop->champion hit explicitly and retain the
    # troop as the transient damage dealer so CardDealtDamage triggers fire.
    # A champion is not a combat card, so a Battle2 effect whose source is the
    # champion and whose remembered target is a troop represents the metadata
    # form "previous target deals damage equal to its ATK to you".  Use the
    # resolved card types rather than display/game text (talent abilities do
    # not always have a card_abilities_meta game_text row).
    source_row = db.execute(
        "SELECT card_type FROM game_cards WHERE session_id=? AND card_uid=?",
        (session.session_id, a)).fetchone()
    target_row = db.execute(
        "SELECT card_type FROM game_cards WHERE session_id=? AND card_uid=?",
        (session.session_id, d)).fetchone()
    source_is_champion = source_row is None or str(source_row[0]).lower() == "champion"
    target_is_troop = target_row is not None and "troop" in str(target_row[0]).lower()
    if source_is_champion and target_is_troop:
        datk = _card_atk(db, session, d, bstate)
        previous_dealer = (bstate or {}).get("resolving_source_uid")
        bstate["resolving_source_uid"] = d
        try:
            result = _deal_damage(game, session, db, handler, pl_t, ai_t,
                                  bstate, a, datk)
        finally:
            if previous_dealer is None:
                bstate.pop("resolving_source_uid", None)
            else:
                bstate["resolving_source_uid"] = previous_dealer
        logs.append(f"{hex(d)} deals {datk} to you -> {result}")
        return "; ".join(logs)
    atk = _card_atk(db, session, a, bstate)
    logs.append(f"{hex(a)} deals {atk} to {hex(d)} -> "
                f"{_deal_damage(game, session, db, handler, pl_t, ai_t, bstate, d, atk)}")
    if "battles" in low:
        datk = _card_atk(db, session, d, bstate)
        logs.append(f"{hex(d)} deals {datk} to {hex(a)} -> "
                    f"{_deal_damage(game, session, db, handler, pl_t, ai_t, bstate, a, datk)}")
    return "; ".join(logs)


@leaf_register("GiveBonusTurnAbilityEffectTemplate")
def _leaf_bonus_turn(game, session, db, handler, pl_t, ai_t, bstate,
                     effect_guid, param):
    """Take an additional turn after this one."""
    owner = int((bstate or {}).get("resolving_owner_id", 0))
    side = "player" if owner else "ai"
    bstate["bonus_turn"] = side
    # PvP consumes the owning player id at the turn boundary.  Keep the
    # existing side marker for the Practice engine, where player/AI is the
    # authoritative ownership model.
    bstate["bonus_turn_pid"] = owner
    return f"bonus turn queued for {side}"


@leaf_register("SacrificeCardAbilityEffectTemplate")
def _leaf_sacrifice(game, session, db, handler, pl_t, ai_t, bstate,
                    effect_guid, param):
    """Sacrifice the resolved target (or the source card)."""
    from .kill_troop import kill_troop
    target = _resolve_leaf_target(bstate)
    if target is None:
        return "sacrifice: no target"
    kill_troop(game, session, db, handler, pl_t, ai_t, int(target),
               bstate, cause="sacrifice")
    return f"sacrificed {hex(int(target))}"


@leaf_register("TransformSelfAbilityEffectTemplate")
def _leaf_transform_self(game, session, db, handler, pl_t, ai_t, bstate,
                         effect_guid, param):
    """Transform the source into a metadata-linked card template."""
    from .transform import transform_card
    source = (bstate or {}).get("resolving_source_uid")
    if source is None:
        return "transform self: no source"
    # TransformSelf is intrinsically anchored to the resolving source.  Do
    # not reuse a stale player_mod_target/player_spell_target from an earlier
    # ability; doing so can replace a Plant Garden with the champion template
    # instead of one of its metadata-linked plant choices.
    new_tpl = None
    linked = _linked_template_guids_from_metadata(db, bstate)
    if linked:
        # The extracted record does not expose the printed probability as a
        # structured variable.  Choose among the linked templates rather than
        # silently selecting the first one every time.
        new_tpl = random.choice(linked)
    if not new_tpl:
        return "transform self: no template"
    transform_card(handler, game, session, pl_t, ai_t, int(source), new_tpl,
                   bstate=bstate)
    return f"transformed self -> {new_tpl[:8]}"


@leaf_register("StoreNameAbilityEffectTemplate")
def _leaf_store_name(game, session, db, handler, pl_t, ai_t, bstate,
                     effect_guid, param):
    """Remember the resolved target's card name for later effects."""
    target = _resolve_leaf_target(bstate)
    if target is None:
        return "store name: no target"
    trow = db.execute(
        "SELECT ct.name FROM game_cards gc JOIN card_templates ct "
        "ON ct.guid = gc.template_guid WHERE gc.session_id=? AND gc.card_uid=?",
        (session.session_id, int(target))).fetchone()
    name = trow[0] if trow else ""
    ag = (bstate or {}).get("resolving_ability", "")
    (bstate or {}).setdefault("stored_names", {}).setdefault(ag, []).append(name)
    return f"stored name '{name}'"


@leaf_register("CreateTokenCopyAbilityEffectTemplate")
def _leaf_create_token_copy(game, session, db, handler, pl_t, ai_t, bstate,
                            effect_guid, param):
    """Create a replica of the resolved target troop (into hand per the text
    "put it into your hand", else the warzone)."""
    import re as _re
    text = _ability_text(db, bstate) or ""
    target = _resolve_leaf_target(bstate)
    if target is None:
        return "copy: no target"
    trow = db.execute(
        "SELECT template_guid FROM game_cards WHERE session_id=? AND card_uid=?",
        (session.session_id, int(target))).fetchone()
    if not trow:
        return "copy: target template missing"
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
    count = 1
    m = _re.search(r'(one|two|three|four|five|\d+)', text.lower())
    if m:
        count = int(m.group(1)) if m.group(1).isdigit() else words.get(m.group(1), 1)
    into_hand = "into your hand" in text.lower()
    owner = int((bstate or {}).get("resolving_owner_id", 0))
    tpl = trow[0]
    tpl_row = db.execute(
        "SELECT card_type, abilities_json, attributes FROM card_templates WHERE guid=?",
        (tpl,)).fetchone()
    created = 0
    created_uids = []
    for i in range(count):
        next_id = db.execute(
            "SELECT COALESCE(MAX(id), 10000) + 1 FROM game_cards WHERE session_id=?",
            (session.session_id,)).fetchone()[0]
        card_uid = next_game_card_uid(db, session.session_id)
        created_uids.append(card_uid)
        loc = "hand" if into_hand else "warzone"
        db.execute(
            "INSERT INTO game_cards (id, session_id, user_id, card_uid, template_guid, "
            "card_template_id, location, position, card_state, card_abilities, "
            "card_type, card_attributes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (next_id, session.session_id, owner, card_uid, tpl, tpl, loc, 0, 0,
             tpl_row[1], tpl_row[0], tpl_row[2]))
        scid = game_engine.SessionCardId(game_engine.UID(card_uid))
        _tpl2, ct2, _n2, cost2, atk2, def2, _g2 = handler._card_full_data(
            game, scid, tpl)
        coll = game_engine.ECardCollections.Hand if into_hand else game_engine.ECardCollections.Warzone
        game.push_card_moved(scid, pl_t if owner else ai_t, coll,
                             game_engine.ECardLocations.Top, 1)
        game.push_card_updated(scid, pl_t if owner else ai_t, coll, ct2,
                               template_id=tpl, cost=cost2, attack=atk2,
                               defense=def2, nulling=False)
        created += 1
    db.commit()
    # Replica cards fire their own CardCreatedEvent abilities too.
    if created:
        from .triggers import resolve_triggers
        for card_uid in created_uids:
            resolve_triggers(db, handler, game, session, pl_t, ai_t, bstate,
                             "CardCreatedEvent", int(card_uid), owner,
                             zones=())
    return f"copied {created}x {tpl[:8]} {'to hand' if into_hand else 'to warzone'}"


@leaf_register("RevokeAbilityEffectTemplate")
def _leaf_revoke(game, session, db, handler, pl_t, ai_t, bstate, effect_guid, param):
    """Remove a granted ability from the resolved target card."""
    import json as _json
    target = _resolve_leaf_target(bstate)
    if target is None:
        return "revoke: no target"
    # RevokeAbilityEffectTemplate stores no parameter for the normal
    # "This loses this power" form.  The active BOM ability is the power to
    # remove; an explicit parameter remains supported for generated effects.
    revoked = (param or (bstate or {}).get("resolving_ability") or "").strip().lower()
    if not revoked:
        return "revoke: no ability guid in param"
    row = db.execute(
        "SELECT card_abilities FROM game_cards WHERE session_id=? AND card_uid=?",
        (session.session_id, int(target))).fetchone()
    if not row:
        return "revoke: target not found"
    try:
        ab = _json.loads(row[0] or "[]")
    except Exception:
        ab = []
    if revoked in ab:
        ab.remove(revoked)
        db.execute(
            "UPDATE game_cards SET card_abilities=? WHERE session_id=? AND card_uid=?",
            (_json.dumps(ab), session.session_id, int(target)))
        db.commit()
    _push_card_state(game, session, db, handler, pl_t, ai_t, int(target),
                     _state_of(db, session, int(target)))
    return f"revoked {revoked[:8]} from {hex(int(target))}"


@leaf_register("CreateAndCastSpellAbilityEffectTemplate")
def _leaf_create_cast_spell(game, session, db, handler, pl_t, ai_t, bstate,
                            effect_guid, param):
    """Copy the resolved spell and cast it (e.g. Chimes of the Zodiac
    "When you play an action, copy it.")."""
    import json as _json
    target = ((bstate or {}).get("card_cast_copy_target")
              or _resolve_leaf_target(bstate))
    if target is None:
        return "copy spell: no target"
    trow = db.execute(
        "SELECT template_guid FROM game_cards WHERE session_id=? AND card_uid=?",
        (session.session_id, int(target))).fetchone()
    if not trow:
        return "copy spell: target template missing"
    tpl = trow[0]
    tpl_row = db.execute(
        "SELECT card_type, abilities_json, attributes FROM card_templates WHERE guid=?",
        (tpl,)).fetchone()
    if not tpl_row:
        return "copy spell: template not found"
    owner = int((bstate or {}).get("resolving_owner_id", 0))
    next_id = db.execute(
        "SELECT COALESCE(MAX(id), 10000) + 1 FROM game_cards WHERE session_id=?",
        (session.session_id,)).fetchone()[0]
    card_uid = next_game_card_uid(db, session.session_id)
    db.execute(
        "INSERT INTO game_cards (id, session_id, user_id, card_uid, template_guid, "
        "card_template_id, location, position, card_state, card_abilities, "
        "card_type, card_attributes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (next_id, session.session_id, owner, card_uid, tpl, tpl, "CastSpells",
         0, 0, tpl_row[1], tpl_row[0], tpl_row[2]))
    db.commit()
    try:
        ags = _json.loads(tpl_row[1] or "[]")
    except Exception:
        ags = []
    scid = game_engine.SessionCardId(game_engine.UID(card_uid))
    _tpl2, ct2, _n2, cost2, atk2, def2, _g2 = handler._card_full_data(
        game, scid, tpl)
    game.push_card_moved(scid, pl_t if owner else ai_t,
                         game_engine.ECardCollections.CastSpells,
                         game_engine.ECardLocations.Top, 0)
    game.push_card_updated(scid, pl_t if owner else ai_t,
                           game_engine.ECardCollections.CastSpells, ct2,
                           template_id=tpl, cost=cost2, attack=atk2, defense=def2)
    from .triggers import resolve_played_spell as _resolve_spell
    logs = _resolve_spell(game, session, db, handler, pl_t, ai_t, bstate, ags)
    from db import db_discard_card
    db_discard_card(session.session_id, card_uid, connection=db)
    return f"copied+cast {tpl[:8]}: {logs}"


@leaf_register("DestroyCardByDefenseAbilityEffectTemplate")
def _leaf_destroy_by_defense(game, session, db, handler, pl_t, ai_t, bstate,
                             effect_guid, param):
    """Tectonic Break: each warzone troop is destroyed unless it beats its
    survival roll (10% chance to survive per point of DEF)."""
    import random as _rnd
    from .kill_troop import kill_troop
    rows = db.execute(
        "SELECT card_uid FROM game_cards WHERE session_id=? "
        "AND location='warzone' AND card_type LIKE '%Troop%'",
        (session.session_id,)).fetchall()
    destroyed = 0
    for (uid,) in rows:
        trow = db.execute(
            "SELECT ct.defense, gc.card_defense_mod FROM game_cards gc "
            "JOIN card_templates ct ON ct.guid = gc.template_guid "
            "WHERE gc.session_id=? AND gc.card_uid=?",
            (session.session_id, int(uid))).fetchone()
        def_ = (trow[0] or 0) + (trow[1] or 0) if trow else 0
        if _rnd.random() > 0.10 * def_:
            kill_troop(game, session, db, handler, pl_t, ai_t, int(uid),
                       bstate, cause="effect")
            destroyed += 1
    return f"destroyed {destroyed}/{len(rows)}"


@leaf_register("TransformCardAtRandomAbilityEffectTemplate")
def _leaf_transform_random(game, session, db, handler, pl_t, ai_t, bstate,
                           effect_guid, param):
    """Transform the target into a random card matching its typed filter.

    ``m_Filter`` is the authoritative candidate pool.  Inferring only
    Artifact/Troop from display text misses same-shard, same-cost,
    same-rarity, and "another card" transforms.
    """
    from .transform import transform_card
    from .targeting import evaluate_card_filter, shards_from_threshold
    target = _resolve_leaf_target(bstate)
    if target is None:
        return "transform random: no target"
    typed = effect_template(effect_guid) or {}
    filter_json = typed.get("m_Filter")
    if not isinstance(filter_json, dict):
        return "transform random: no typed filter"
    source_uid = (bstate or {}).get("resolving_source_uid")
    source_row = db.execute(
        "SELECT gc.template_guid, gc.card_type, gc.location, gc.user_id, "
        "gc.card_state, gc.permanent_buffs, ct.name, ct.cost, ct.rarity, "
        "ct.threshold_json, ct.subtype, ct.attributes, gc.card_attributes "
        "FROM game_cards gc JOIN card_templates ct ON ct.guid=gc.template_guid "
        "WHERE gc.session_id=? AND gc.card_uid=?",
        (session.session_id, int(source_uid))).fetchone() \
        if source_uid is not None else None

    def card_record(row, uid):
        data = {
            "card_uid": int(uid), "template_guid": row[0],
            "name": row[6] or "",
            "card_type": row[1] or "", "location": row[2] or "",
            "user_id": int(row[3] or 0), "state": int(row[4] or 0),
            "cost": int(row[7] or 0), "rarity": row[8] or "",
            "shards": shards_from_threshold(row[9]),
            "subtype": row[10] or "", "attributes": int(row[11] or 0) |
            int(row[12] or 0),
        }
        try:
            saved = json.loads(row[5] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            saved = {}
        data["counters"] = saved.get("counters") or {}
        data["counter_guids"] = saved.get("counter_guids") or {}
        return data

    source_card = card_record(source_row, source_uid) if source_row else None
    if source_card:
        ability_guid = (bstate or {}).get("resolving_ability", "")
        try:
            raw_row = db.execute(
                "SELECT raw_json FROM card_abilities_meta "
                "WHERE ability_guid=?", (ability_guid,)).fetchone()
            raw = json.loads(raw_row[0] or "{}") if raw_row else {}
            source_card["ability_variables"] = {
                str(v.get("m_Name")): int(v.get("m_DefaultValue", 0) or 0)
                for v in raw.get("m_Variables", [])
                if isinstance(v, dict) and v.get("m_Name")
            }
        except (TypeError, ValueError, json.JSONDecodeError):
            source_card["ability_variables"] = {}
        source_card["cost_delta"] = 0
        # HasSourceCastingCostFilter's AddValue is an EffectField.  Its
        # concrete variable is resolved from the active ability metadata.
        add_value = filter_json.get("m_AddValue")
        if isinstance(add_value, dict):
            variable = add_value.get("m_InputVariableName") or \
                add_value.get("m_VariableName")
            if variable:
                source_card["cost_delta"] = int(
                    source_card["ability_variables"].get(variable, 0))
    rows = db.execute(
        "SELECT guid, name, card_type, cost, rarity, threshold_json, subtype, "
        "attributes FROM card_templates WHERE is_pve=0").fetchall()
    candidates = []
    cant_same = bool(typed.get("m_CantBeSameCard"))
    for row in rows:
        candidate = {
            "card_uid": 0, "template_guid": row[0], "name": row[1] or "",
            "card_type": row[2] or "",
            "cost": int(row[3] or 0), "rarity": row[4] or "",
            "shards": shards_from_threshold(row[5]),
            "subtype": row[6] or "", "attributes": int(row[7] or 0),
            "location": "", "user_id": source_card.get("user_id", 0)
            if source_card else 0,
        }
        if cant_same and source_card and row[0].lower() == \
                source_row[0].lower():
            continue
        if evaluate_card_filter(candidate, filter_json, source_uid,
                                source_card=source_card):
            candidates.append(row[0])
    if not candidates:
        return "transform random: no candidates"
    new_tpl = random.choice(candidates)
    transform_card(handler, game, session, pl_t, ai_t, int(target), new_tpl,
                   bstate=bstate)
    return f"transformed {hex(int(target))} -> random {new_tpl[:8]}"

@leaf_register("TransformCardAbilityEffectTemplate")
def _leaf_transform(game, session, db, handler, pl_t, ai_t, bstate, effect_guid, param):
    """Transform a card into another card."""
    from .transform import transform_card
    import re as _re
    if (bstate or {}).get("_skip_transform"):
        return "transform skipped (gate not met)"
    ability_guid = (bstate or {}).get("resolving_ability", "")
    game_text = ""
    if ability_guid:
        g_row = db.execute(
            "SELECT game_text FROM card_abilities_meta WHERE ability_guid=?",
            (ability_guid,)).fetchone()
        if g_row:
            game_text = g_row[0] or ""
    # The transform effect carries the destination template directly in
    # m_CardTemplateId.  This is what the client applies; use the display link
    # only for old extracted rows that predate that field.
    new_tpl = effect_template_value(
        db, bstate, effect_guid, "m_CardTemplateId", "")
    if new_tpl and str(new_tpl).lower() != "0" * 36:
        new_tpl = str(new_tpl).lower()
    else:
        new_tpl = ""
    # The ability text can link the SOURCE card first ("...remove all counters
    # from all your <a data=<source>>Incantations</a>... Transform them into
    # <a data=<target>>Sentinels</a>") — the transform target is the LAST card
    # link, matching the old trigger-path behaviour.
    if not new_tpl:
        links = _re.findall(r'data=([0-9a-fA-F]{8}-[0-9a-fA-F-]{27})',
                            game_text or "")
        new_tpl = links[-1].lower() if links else None
    if not new_tpl:
        return "transform: no template link in ability text"
    # Incantation-style chains stage every copy (with its zone) when counters
    # were removed — transform them all in place (deck/hand/discard included).
    pending = (bstate or {}).get("pending_transform_cards") or []
    if pending:
        count = 0
        for entry in pending:
            tuid = entry[0] if isinstance(entry, (tuple, list)) else entry
            loc = entry[1] if isinstance(entry, (tuple, list)) and len(entry) > 1 else None
            transform_card(handler, game, session, pl_t, ai_t,
                           int(tuid), new_tpl, keep_zone=True, bstate=bstate)
            count += 1
        bstate.pop("pending_transform_cards", None)
        return f"transform {count} -> {new_tpl[:8]} (keep zone {loc})"
    target_uid = ((bstate or {}).get("player_transform_target")
                  or (bstate or {}).get("player_mod_target")
                  or (bstate or {}).get("player_shift_source")
                  or (bstate or {}).get("resolving_source_uid"))
    if target_uid:
        transform_card(handler, game, session, pl_t, ai_t,
                       int(target_uid), new_tpl, bstate=bstate)
        return f"transformed {hex(int(target_uid))} -> {new_tpl[:8]}"
    return "transform: no target"

@leaf_register("VerdictAbilityEffectTemplate")
def _leaf_verdict(game, session, db, handler, pl_t, ai_t, bstate, effect_guid, param):
    """Apply a verdict effect."""
    return "verdict effect"

@leaf_register("GrantAbilityEffectTemplate")
def _leaf_grant_ability(game, session, db, handler, pl_t, ai_t, bstate, effect_guid, param):
    """Grant an ability to a card — append the ability GUID to the target
    card's game_cards.card_abilities list and push a CardUpdated."""
    import json as _json
    import game_engine as _ge
    from .fields import ability_record

    granted_guids = []
    if param and param.lower() != "00000000-0000-0000-0000-000000000000":
        granted_guids = [param.lower()]
    else:
        granted_guids = list((bstate or {}).get("remembered_powers", {}).get(
            (bstate or {}).get("resolving_ability", ""), []))
    if not granted_guids:
        typed_guid = effect_template_value(
            db, bstate, effect_guid, "m_GrantedAbilityTemplateId", "")
        if typed_guid and typed_guid != "0" * 36:
            granted_guids = [typed_guid]
    if not granted_guids:
        return "grant: no ability GUIDs"
    target_uid = (bstate or {}).get("grant_target")
    if not target_uid:
        return "grant: no target"
    grant_template = effect_template(effect_guid) or {}
    # The client permits repeated copies only when the GrantAbility template
    # explicitly marks the granted ability as non-unique.  Older extracted
    # templates omit that field, so retain the historical unique behavior.
    ability_is_unique = bool(grant_template.get("m_AbilityIsUnique", 1))

    # Append to target card's abilities.
    row = db.execute(
        "SELECT card_abilities, template_guid, user_id, location, card_state "
        "FROM game_cards WHERE session_id=? AND card_uid=?",
        (session.session_id, int(target_uid))).fetchone()
    if not row:
        # Champions are not represented by game_cards in the live session.
        # Still retain the granted ability on the handler and immediately
        # resolve a newly-granted GameStarted ability (e.g. Taming Sphere).
        for attr, owner in (("_player_champ_scid", handler.user_profile["id"]
                             if handler.user_profile else 0),
                            ("_ai_champ_scid", 0)):
            champ = getattr(handler, attr, None)
            if champ is None or int(champ.uid.uid64) != int(target_uid):
                continue
            dynamic = getattr(handler, "_champion_granted_ability_guids", None)
            if dynamic is None:
                dynamic = handler._champion_granted_ability_guids = {}
            champ_key = int(target_uid)
            current = dynamic.setdefault(champ_key, [])
            added = []
            for granted_guid in granted_guids:
                if not db.execute(
                        "SELECT 1 FROM card_abilities_meta WHERE ability_guid=?",
                        (granted_guid,)).fetchone() and not ability_record(
                            db, granted_guid):
                    _log(f"    GrantAbility: {granted_guid[:8]} not in metadata")
                    continue
                if ability_is_unique and granted_guid in current:
                    continue
                current.append(granted_guid)
                added.append(granted_guid)

            # A newly granted ability that also listens for GameStarted must
            # resolve once during the current dispatch.  Other grants are
            # retained for their own future event (Ridge Raiders is a death
            # trigger and must not deal damage during setup).
            if added and (bstate or {}).get("event_type") == "GameStartedEvent":
                from .fields import ability_record
                from .triggers import _resolve_ability_bom
                for granted_guid in added:
                    child = ability_record(db, granted_guid)
                    child_event = str((child.get("m_TriggerEventType") or {}).get(
                        "m_InternalType") or "")
                    if child_event.endswith("GameStartedEvent"):
                        _resolve_ability_bom(
                            db, handler, game, session, pl_t, ai_t, bstate,
                            granted_guid, int(target_uid), "", target_uid=None,
                            source_owner_uid=owner)
            return f"grant champion ability ({len(added)})"
        return "grant: target card not found"
    try:
        ab_list = _json.loads(row[0] or "[]")
    except Exception:
        ab_list = []
    added = []
    for granted_guid in granted_guids:
        exists = db.execute(
            "SELECT 1 FROM card_abilities_meta WHERE ability_guid=?",
            (granted_guid,)).fetchone()
        if not exists:
            _log(f"    GrantAbility: {granted_guid[:8]} not in DB — extraction may be stale")
            continue
        if ability_is_unique and granted_guid in ab_list:
            continue
        # AbilityIsUnique is false for Spider's Nest.  Each cast creates a
        # separate ability instance, even though both instances reference the
        # same metadata GUID; retaining duplicate GUIDs lets trigger
        # resolution fire once per grant.
        ab_list.append(granted_guid)
        added.append(granted_guid)
    db.execute(
        "UPDATE game_cards SET card_abilities=? WHERE session_id=? AND card_uid=?",
        (_json.dumps(ab_list), session.session_id, int(target_uid)))
    db.commit()

    # Push CardUpdated so the client renders the new ability button.
    scid = _ge.SessionCardId(_ge.UID(int(target_uid)))
    from ._shared import card_collection_for_location, owner_uid
    owner = owner_uid(row[2], pl_t, ai_t, bstate)
    tpl_guid, ct, _n, cost, atk, def_, _gem = handler._card_full_data(
        game, scid, row[1], None)
    game.push_card_updated(scid, owner, card_collection_for_location(row[3]), ct,
                           attack=atk, defense=def_, cost=cost,
                           state=int(row[4] or 0), template_id=tpl_guid,
                           nulling=(row[3] == "deck"))
    return f"granted {len(added)} ability(s) to {hex(int(target_uid))}"


@leaf_register("RegisterTriggerAbilityEffectTemplate")
def _leaf_register_trigger(game, session, db, handler, pl_t, ai_t, bstate,
                           effect_guid, param):
    """Register a dynamic trigger on a card for this battle instance.

    Registered ability templates are kept separately from the card's printed
    list so a temporary control-change trigger does not become permanent card
    data.  The trigger dispatcher consumes this list when its metadata is
    available in the extracted seed.
    """
    target = _resolve_leaf_target(bstate)
    template_id = effect_template_value(
        db, bstate, effect_guid, "m_TriggerAbilityTemplateId", "")
    if target is None or not template_id:
        return "register trigger: missing target or template"
    registered = (bstate or {}).setdefault("registered_triggers", {})
    values = registered.setdefault(str(int(target)), [])
    if template_id not in values:
        values.append(template_id)
    return f"registered trigger {template_id[:8]} on {hex(int(target))}"


@leaf_register("CopyAbilityEffectTemplate")
def _leaf_copy_ability(game, session, db, handler, pl_t, ai_t, bstate,
                       effect_guid, param):
    """Put a copy of the CardActivatedEvent ability back on the chain."""
    original = (bstate or {}).get("card_activated_item")
    if not original or not original.get("ability_guid"):
        return "copy ability: no activated ability"
    import battle_engine as _be
    instance_id = int((bstate or {}).get("_next_instance_id", 1))
    bstate["_next_instance_id"] = instance_id + 1
    copied = {
        "kind": "ability", "ability_guid": original["ability_guid"],
        "source_uid": original.get("source_uid"),
        "target_uid": original.get("target_uid"),
        "instance_id": instance_id,
    }
    _be.stack_push(bstate, copied)
    source = original.get("source_uid")
    if source is not None:
        game.push_ability_on_chain(
            game_engine.SessionCardId(game_engine.UID(int(source))),
            game_engine.ResourceId.from_str(str(original["ability_guid"])),
            ability_instance_id=instance_id)
    return f"copied ability {str(original['ability_guid'])[:8]}"

@leaf_register("PlayCardAbilityEffectTemplate")
def _leaf_play_card(game, session, db, handler, pl_t, ai_t, bstate, effect_guid, param):
    """Play a card for free using the effect's gamedata target.

    The target template for Chlorophyllia/its nested ability is a random
    Wild Shard in the controller's deck.  That is different from the common
    source-card case (for example, a card that plays itself after being
    drawn), so resolve the target template first and use the normal resource
    play events/state changes for a selected resource.
    """
    parent = db.execute(
        "SELECT ability_guid FROM ability_effects "
        "WHERE effect_guid=? AND effect_type='PlayCardAbilityEffectTemplate' "
        "LIMIT 1", (effect_guid,)).fetchone()
    if parent:
        target_row = db.execute(
            "SELECT target_template_ids FROM card_abilities_meta "
            "WHERE ability_guid=?", (parent[0],)).fetchone()
        try:
            target_ids = json.loads(target_row[0] or "[]") if target_row else []
        except (TypeError, ValueError, json.JSONDecodeError):
            target_ids = []
        if target_ids:
            # A PlayCard effect can have a target template for two very
            # different purposes.  Resource abilities use an auto-target
            # describing a random Resource in the controller's deck.  A
            # triggered card such as Angel of Dawn uses
            # AbilityTriggerCardTargetTemplate, where the target is the card
            # that caused the event (Angel itself in this case).  Only the
            # former should enter the deck/resource selection path below.
            # Treating every target template as a deck resource made drawn
            # troops stay in hand with "no matching target in deck".
            from .targeting import legal_targets, target_template

            target = target_template(db, target_ids[0])
            target_kind = (target or {}).get("target_kind") or ""
            is_random_deck_target = False
            if target_kind == "AbilityTargetTemplate" and target:
                try:
                    filter_json = json.loads(target.get("filter_json") or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    filter_json = {}

                def _has_filter(node, wanted):
                    if isinstance(node, dict):
                        if str(node.get("_t", "")).rsplit(".", 1)[-1] == wanted:
                            return node
                        for child in node.values():
                            found = _has_filter(child, wanted)
                            if found is not None:
                                return found
                    elif isinstance(node, list):
                        for child in node:
                            found = _has_filter(child, wanted)
                            if found is not None:
                                return found
                    return None

                zone_filter = _has_filter(filter_json, "InZone")
                # The selected card may be identified by any metadata filter
                # (for example Wild Shard uses IsCardName rather than
                # IsResource).  The target template, not a card name/type
                # check here, determines that this is a random deck target.
                is_random_deck_target = (
                    bool(target.get("is_random_target")) and
                    zone_filter is not None and
                    str(zone_filter.get("m_Collection", "")).lower() == "deck")

            if is_random_deck_target:

                owner_id = int((bstate or {}).get("resolving_owner_id", 0) or 0)
                source_uid = (bstate or {}).get("resolving_source_uid")
                candidates = legal_targets(
                    db, session.session_id, owner_id, target_ids[0], source_uid,
                    both_players=False)
                if candidates:
                    selected_uid = int(random.choice(candidates))
                    try:
                        selected = db.execute(
                            "SELECT gc.template_guid, gc.card_template_id, "
                            "ct.card_type, ct.current_resources_granted, "
                            "ct.max_resources_granted, ct.threshold_json, "
                            "ct.abilities_json "
                            "FROM game_cards gc JOIN card_templates ct "
                            "ON ct.guid=gc.template_guid "
                            "WHERE gc.session_id=? AND gc.card_uid=? ",
                            (session.session_id, selected_uid)).fetchone()
                    except Exception as exc:
                        # A few focused test databases predate the grant columns;
                        # production/static.py always has them.  Keep the test
                        # harness compatible without weakening the live query.
                        if "current_resources_granted" not in str(exc):
                            raise
                        selected = db.execute(
                            "SELECT gc.template_guid, gc.card_template_id, "
                            "ct.card_type, ct.threshold_json, ct.abilities_json "
                            "FROM game_cards gc JOIN card_templates ct "
                            "ON ct.guid=gc.template_guid "
                            "WHERE gc.session_id=? AND gc.card_uid=? ",
                            (session.session_id, selected_uid)).fetchone()
                        if selected:
                            selected = (selected[0], selected[1], selected[2],
                                        1, 1, selected[3], selected[4])
                    # The target template chooses the candidate; the card's
                    # authoritative type determines whether this resolver
                    # branch should apply resource-pool bookkeeping.
                    if selected and selected[2] == "Resource":
                        (tpl_guid, instance_id, _card_type, current_grant,
                         max_grant, threshold_json, resource_abilities) = selected
                        current_grant = int(current_grant or 0)
                        max_grant = int(max_grant or 0)
                        # Imported/legacy resource rows predate the explicit
                        # grant columns; keep their behavior identical to a
                        # normal basic shard.
                        if not current_grant and not max_grant:
                            current_grant = max_grant = 1
                        db.execute(
                            "UPDATE game_cards SET location='PlayedResources', "
                            "position=9999 WHERE session_id=? AND card_uid=?",
                            (session.session_id, selected_uid))
                        db.commit()

                        pvp = bool((bstate or {}).get("pvp"))
                        side = "player" if pvp else ("player" if owner_id else "ai")
                        resource_key = f"{side}_resources"
                        total_key = f"{side}_total_resources"
                        charge_key = f"{side}_charges"
                        threshold_key = f"{side}_threshold"
                        bstate[resource_key] = int(bstate.get(resource_key, 0)) + current_grant
                        bstate[total_key] = int(bstate.get(total_key, 0)) + max_grant
                        threshold_flags = []
                        charge_grant = 0
                        try:
                            resource_ability_guids = json.loads(
                                resource_abilities or "[]")
                        except (TypeError, ValueError, json.JSONDecodeError):
                            resource_ability_guids = []
                        for resource_ability in resource_ability_guids:
                            for effect in db.execute(
                                    "SELECT effect_type, param FROM ability_effects "
                                    "WHERE ability_guid=? ORDER BY effect_order",
                                    (str(resource_ability).lower(),)).fetchall():
                                if effect[0] != "CardModifierAbilityEffectTemplate":
                                    continue
                                try:
                                    modifier = json.loads(effect[1] or "{}")
                                except (TypeError, ValueError, json.JSONDecodeError):
                                    modifier = {}
                                prop = modifier.get("property")
                                amount = int(modifier.get("amount") or 0)
                                if prop == "chargepoints":
                                    charge_grant += amount
                                elif prop == "threshold":
                                    import re as _re
                                    match = _re.search(
                                        r"\[([A-Za-z]+)\]", modifier.get("text", ""))
                                    if match:
                                        flag = game_engine.SHARD_TO_FLAG.get(
                                            match.group(1).lower(), 0)
                                        if flag:
                                            threshold_flags.append((flag, amount))
                        if not charge_grant:
                            charge_grant = 1
                        bstate[charge_key] = int(bstate.get(charge_key, 0)) + charge_grant
                        threshold = bstate.setdefault(threshold_key, {})
                        scid = game_engine.SessionCardId(game_engine.UID(selected_uid))
                        card_owner = owner_uid(owner_id, pl_t, ai_t, bstate)
                        _tpl, card_type, _name, _cost, _atk, _def, _gem = \
                            handler._card_full_data(
                                game, scid, tpl_guid, instance_id)
                        game.push_card_updated(
                            scid, card_owner, game_engine.ECardCollections.PlayedResources,
                            game_engine.ECardTypes.Resource, template_id=tpl_guid)
                        game.push_resource_card_played(scid, card_owner, free=True)

                        if side == "player":
                            game.player_resources = bstate[resource_key]
                            game.player_total_resources = bstate[total_key]
                            game.player_charges = bstate[charge_key]
                        else:
                            game.ai_resources = bstate[resource_key]
                            game.ai_total_resources = bstate[total_key]
                            game.ai_charges = bstate[charge_key]

                        ev_cur = game_engine.PlayerCurrentResourcePoolChangedSessionEventArgs()
                        ev_cur.player_id = card_owner
                        ev_cur.operation = 1
                        ev_cur.delta = current_grant
                        ev_cur.new_value = bstate[resource_key]
                        game._push(ev_cur)
                        ev_tot = game_engine.PlayerTotalResourcePoolChangedSessionEventArgs()
                        ev_tot.player_id = card_owner
                        ev_tot.operation = 1
                        ev_tot.delta = max_grant
                        ev_tot.new_value = bstate[total_key]
                        game._push(ev_tot)
                        for flag, amount in threshold_flags:
                            current_threshold = threshold.get(flag)
                            if current_threshold is None:
                                current_threshold = threshold.get(str(flag), 0)
                            threshold[flag] = int(current_threshold or 0) + amount
                            ev_th = game_engine.PlayerResourceThresholdChangedSessionEventArgs()
                            ev_th.player_id = card_owner
                            ev_th.color = flag
                            ev_th.operation = 1
                            ev_th.delta = amount
                            ev_th.new_value = threshold[flag]
                            game._push(ev_th)
                        if side == "player":
                            game.player_threshold = dict(threshold)
                        else:
                            game.ai_threshold = dict(threshold)
                        ev_chg = game_engine.ChampionChargePointsChangedSessionEventArgs()
                        ev_chg.player_id = card_owner
                        ev_chg.operation = 1
                        ev_chg.delta = 1
                        ev_chg.new_value = bstate[charge_key]
                        game._push(ev_chg)
                        from .triggers import resolve_gain_charge_triggers
                        resolve_gain_charge_triggers(
                            db, handler, game, session, pl_t, ai_t, bstate,
                            owner_id)
                        return (f"played free resource {selected_uid} "
                                f"(+{current_grant} current/+{max_grant} total, "
                                f"thresholds={threshold_flags}, charge={charge_grant})")

                return "play for free: no matching target in deck"

    from db import (
        db_set_card_played_to_zone,
        db_card_set_warzone_arrival,
        db_set_card_resolved_at,
    )
    src_uid = (bstate or {}).get("resolving_source_uid")
    if src_uid is None:
        return "play for free: no source"
    row = db.execute(
        "SELECT template_guid, card_type FROM game_cards WHERE session_id=? AND card_uid=?",
        (session.session_id, int(src_uid))).fetchone()
    if not row:
        return "play for free: source not found"
    tpl_guid, ctype = row
    if ctype != "Troop":
        return f"play for free: {ctype} not supported yet"
    scid = game_engine.SessionCardId(game_engine.UID(int(src_uid)))
    _tpl, ct, _n, cost, atk, def_, _g = handler._card_full_data(
        game, scid, tpl_guid)
    # Hand -> CastSpells (the stack), then auto-resolve to Warzone (the AI
    # opponent always passes), exactly like a normally played troop.
    db_set_card_played_to_zone(session.session_id, int(src_uid), 'CastSpells')
    game.push_card_updated(scid, pl_t, game_engine.ECardCollections.CastSpells,
                           ct, template_id=tpl_guid, cost=cost, attack=atk,
                           defense=def_)
    game.push_card_moved(scid, pl_t, game_engine.ECardCollections.CastSpells,
                         game_engine.ECardLocations.Top, 0)
    game.push_troop_card_played(scid, pl_t)
    game.push_green_light(ai_t, game_engine.EPriorityContext.ResolveTopOfChain)
    db_card_set_warzone_arrival(session.session_id, int(src_uid))
    db_set_card_resolved_at(session.session_id, int(src_uid),
                            handler._next_resolve_counter(session))
    game.push_card_moved(scid, pl_t, game_engine.ECardCollections.Warzone,
                         game_engine.ECardLocations.Top, 0)
    game.push_card_updated(scid, pl_t, game_engine.ECardCollections.Warzone, ct,
                           template_id=tpl_guid, cost=cost, attack=atk,
                           defense=def_)
    # The free-played troop fires its own enters-play triggers (e.g. a
    # Scrivener already on board heals when it resolves).
    from .triggers import resolve_enters_play_triggers
    resolve_enters_play_triggers(
        db, handler, game, session, pl_t, ai_t, bstate,
        int(src_uid), (bstate or {}).get("resolving_owner_id", 0), cost)
    return f"played {tpl_guid[:8]} for free"

@leaf_register("FireEventEffectTemplate")
def _leaf_fire_event(game, session, db, handler, pl_t, ai_t, bstate, effect_guid, param):
    """Fire a game event."""
    return "fire event"

@leaf_register("ActivateAbilityEffectTemplate")
def _leaf_invoke(game, session, db, handler, pl_t, ai_t, bstate, effect_guid, param):
    logs = []
    if param:
        for sub in _walk_bom(db, param):
            fn = _LEAFS.get(sub["effect_type"])
            if fn:
                logs.append(fn(game, session, db, handler, pl_t, ai_t, bstate,
                               sub["effect_guid"], sub["param"]))
    return "invoke: " + "; ".join(str(l) for l in logs if l)


@leaf_register("TACAbilityEffectTemplate")
def _leaf_tac(game, session, db, handler, pl_t, ai_t, bstate, effect_guid, param):
    from .tac import tac_function, tac_guid

    if not param:
        return "tac: no serialized data"
    func = tac_function(param)
    guid = tac_guid(param)
    if func == "ShiftAbility" and guid:
        return _shift_power(game, session, db, handler, pl_t, ai_t, bstate, guid)
    if func == "Escalate":
        # "Escalate your cards with the same name as this in all zones"
        # (e.g. Chronic Madness): each copy's EscalationCount grows, so the
        # next ESC-based amount doubles (4 -> 8 -> ...).  The caller's
        # escalation re-render block (player_escalation_uses) pushes the new
        # multiplier onto every copy the caster owns.
        owner = int((bstate or {}).get("resolving_owner_id", 0))
        side = "ai" if owner == 0 else "player"
        key = f"{side}_escalation_uses"
        if (bstate or {}).get("_esc_counted_this_resolution"):
            # An ESC-based leaf earlier in this same resolution already
            # advanced the counter (Ragefire's "Deal ESC:2 damage"): the
            # Escalate operation is the same event, not a second one.
            return "escalate (already counted by ESC leaf)"
        (bstate or {})[key] = int((bstate or {}).get(key, 0)) + 1
        (bstate or {})["_esc_counted_this_resolution"] = True
        return f"escalate {side} (uses={bstate[key]})"
    return f"tac: {func or '?'}"


def _shift_power(game, session, db, handler, pl_t, ai_t, bstate, ability_guid):
    source_uid = (bstate or {}).get("player_shift_source")
    target_uid = (bstate or {}).get("player_shift_target")
    if not source_uid or not target_uid:
        return f"shift: missing source/target (source={source_uid} target={target_uid})"
    handler._shift_ability_between(session, pl_t, ai_t, int(source_uid), int(target_uid), ability_guid, game)
    return f"shift {ability_guid[:8]} {hex(int(source_uid))} -> {hex(int(target_uid))}"


# ---------------------------------------------------------------------------
#  BOM helpers
# ---------------------------------------------------------------------------

def bom_has_leaf(db, ability_guid, leaf_type):
    """True if the ability's BOM (recursively through ActivateAbility leaves)
    contains a leaf effect of ``leaf_type``."""
    seen = set()

    def walk(g):
        if g in seen:
            return False
        seen.add(g)
        for row in _walk_bom(db, g):
            et = row["effect_type"]
            if et == leaf_type:
                return True
            if et == "ActivateAbilityEffectTemplate" and row["param"]:
                if walk(row["param"]):
                    return True
        return False

    return walk(ability_guid)


def bom_has_discard(db, ability_guid):
    """Convenience: does the ability's BOM chain end in a discard effect?"""
    return bom_has_leaf(db, ability_guid, "DiscardCardAbilityEffectTemplate")


def bom_leaf_prompt_data(db, ability_guid, leaf_type):
    """Return ``(leaf_ability_guid, target_template_guid)`` for a BOM leaf.

    ``ActivateAbilityEffectTemplate`` stores the invoked ability GUID in the
    BOM ``param`` column.  Follow that metadata recursively, then resolve the
    leaf's target template from its own ``target_template_ids`` row.  This is
    used by follow-up client prompts (such as choose-and-discard) so protocol
    code does not need to know the GUID of a shared child ability.
    """
    seen = set()

    def walk(guid):
        guid = str(guid or "").lower()
        if not guid or guid in seen:
            return None
        seen.add(guid)
        rows = db.execute(
            "SELECT effect_type, param, target_index FROM ability_effects "
            "WHERE ability_guid=? ORDER BY effect_order", (guid,)).fetchall()
        for effect_type, param, target_index in rows:
            if effect_type == leaf_type:
                row = db.execute(
                    "SELECT target_template_ids FROM card_abilities_meta "
                    "WHERE ability_guid=? LIMIT 1", (guid,)).fetchone()
                try:
                    targets = json.loads(row[0] or "[]") if row else []
                except (TypeError, ValueError, json.JSONDecodeError):
                    targets = []
                try:
                    index = int(target_index)
                except (TypeError, ValueError):
                    index = 0
                if 0 <= index < len(targets):
                    return guid, str(targets[index])
                if targets:
                    return guid, str(targets[0])
                return guid, None
            if effect_type == "ActivateAbilityEffectTemplate" and param:
                found = walk(param)
                if found:
                    return found
        return None

    return walk(ability_guid)
