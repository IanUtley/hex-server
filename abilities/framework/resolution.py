"""Authoritative ability resolution — a Python port of the client's
AbilityInstance / AbilityEffectInstance machinery (``ApplyEffectGroup`` +
``ResolveAutoTarget`` + ``AreApplyContingenciesMet``).

Walks an ability's ``m_AbilityEffectList`` data-driven (ability_effects rows
carrying effect_group_id / condition_id / target_index / effect_instance_id /
contingent_effect_instance_id / secondary_target_index / recalculate_targets /
is_optional / effect_duration / output_variables restored from the gamedata):

* effects are grouped by effect_group_id and each group applies in order,
* each effect is gated by its gamedata condition (ability_effect_conditions),
* ability variables (RandomizeVariable / SetCardIntegerVariable) carry through
  the whole activation,
* an effect's target comes from the activation TargetMap, an auto target
  template (resolved data-driven), or an activation-data prompt (deck search),
* ActivateAbility spawns the child with a FRESH target map — the child resolves
  its own targets against its own templates, exactly like
  ``Session.ActivateAbilityFromEffect``,
* leaves run through the same ``_LEAFS`` executors as the flat BOM walk, so all
  the existing data-driven leaf behaviour (damage, heal, stat mods, moves,
  summons, transforms, counters...) is preserved.
"""

import json
import random

import game_engine

from .condition_engine import ConditionContext, evaluate_effect_condition
from .bom import _LEAFS
from .fields import ability_record, ability_variables, effect_template, resolve_field
from .targeting import (legal_targets, evaluate_card_filter,
                         validate_target_selection)
from ._shared import pvp_champion_uid, pvp_opponent_pid


def _parse_param(param):
    if not param:
        return None
    try:
        d = json.loads(param)
        return d if isinstance(d, dict) else None
    except (ValueError, TypeError):
        return None


def _effect_list(db, ability_guid):
    rows = db.execute(
        "SELECT effect_guid, effect_order, effect_type, param, "
        "effect_group_id, condition_id, target_index, "
        "effect_instance_id, contingent_effect_instance_id, "
        "secondary_target_index, recalculate_targets, is_optional, "
        "effect_duration, output_variables "
        "FROM ability_effects WHERE ability_guid=? ORDER BY effect_order",
        (ability_guid,)).fetchall()
    out = []
    for r in rows:
        out.append({
            "effect_guid": r[0],
            "effect_order": r[1],
            "effect_type": r[2] or "",
            "param": r[3] or "",
            "effect_group_id": int(r[4] or 0),
            "condition_id": r[5] or "",
            "target_index": int(r[6] if r[6] is not None else -1),
            "effect_instance_id": int(r[7] if r[7] is not None else -1),
            "contingent_effect_instance_id": int(r[8] if r[8] is not None else -1),
            "secondary_target_index": int(r[9] if r[9] is not None else -1),
            "recalculate_targets": int(r[10] if r[10] is not None else -1),
            "is_optional": int(r[11] or 0),
            "effect_duration": r[12] or "Instant",
            "output_variables": r[13] or "{}",
        })
    if out:
        # Some development databases contain the BOM rows but predate the
        # parent-level effect metadata columns being populated.  A target
        # index of -1 is valid for targetless effects, but group 0 is never an
        # authored effect group.  Recover the missing wiring from the
        # authoritative AbilityTemplate so conditional branches (such as
        # Skylak's twelve monthly Zodiac effects) cannot all execute.
        if any(effect["effect_group_id"] == 0 for effect in out):
            record = ability_record(db, ability_guid)
            for order, entry in enumerate(
                    record.get("m_AbilityEffectList") or []):
                if order >= len(out) or not isinstance(entry, dict):
                    continue
                effect = out[order]
                def _index(value, default=-1):
                    if value is None:
                        return default
                    try:
                        return int(value)
                    except (TypeError, ValueError):
                        return default
                condition_id = str(
                    (entry.get("m_ConditionId") or {}).get("m_Guid")
                    or "").lower()
                if condition_id == "00000000-0000-0000-0000-000000000000":
                    condition_id = ""
                effect["effect_group_id"] = _index(
                    entry.get("m_EffectGroupId"), effect["effect_group_id"])
                effect["condition_id"] = condition_id
                effect["target_index"] = _index(
                    entry.get("m_TargetTemplateIndex"), effect["target_index"])
                effect["effect_instance_id"] = _index(
                    entry.get("m_EffectInstanceId"),
                    effect["effect_instance_id"])
                effect["contingent_effect_instance_id"] = _index(
                    entry.get("m_ContingentEffectInstanceId"),
                    effect["contingent_effect_instance_id"])
                effect["secondary_target_index"] = _index(
                    entry.get("m_SecondaryTargetIndex"),
                    effect["secondary_target_index"])
                recalc = entry.get("m_RecalculateTargets")
                effect["recalculate_targets"] = {
                    "True": 1, "False": 0, "UseDefault": -1,
                }.get(str(recalc), effect["recalculate_targets"])
                effect["is_optional"] = _index(
                    entry.get("m_IsOptional"), effect["is_optional"])
                effect["effect_duration"] = (
                    entry.get("m_EffectDuration") or effect["effect_duration"])
                effect["output_variables"] = json.dumps(
                    entry.get("m_OutputVariables") or {})
        return out
    # Recover an unmaterialized transitive grant directly from the extracted
    # AbilityTemplate.  This keeps the resolver data-driven for abilities that
    # are referenced only by a GrantAbility child.
    record = ability_record(db, ability_guid)
    for order, entry in enumerate(record.get("m_AbilityEffectList") or []):
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
            param = json.dumps({"property": prop, "amount": 0,
                                "duration": entry.get("m_EffectDuration",
                                                         "Instant")})
        out.append({
            "effect_guid": effect_guid,
            "effect_order": order,
            "effect_type": effect_type,
            "param": param,
            "effect_group_id": int(entry.get("m_EffectGroupId", order + 1) or 0),
            "condition_id": str((entry.get("m_ConditionId") or {}).get(
                "m_Guid") or "").lower(),
            # Zero is the first (and most common) target-template index; do
            # not turn it into -1 through a truthiness fallback.
            "target_index": int(entry.get("m_TargetTemplateIndex", -1)
                                 if entry.get("m_TargetTemplateIndex") is not None
                                 else -1),
            "effect_instance_id": int(entry.get("m_EffectInstanceId", order) or 0),
            "contingent_effect_instance_id": int(entry.get(
                "m_ContingentEffectInstanceId", -1) or -1),
            "secondary_target_index": int(entry.get("m_SecondaryTargetIndex", -1) or -1),
            "recalculate_targets": 1 if str(entry.get("m_RecalculateTargets", "")).lower() == "true" else 0,
            "is_optional": int(entry.get("m_IsOptional", 0) or 0),
            "effect_duration": entry.get("m_EffectDuration") or "Instant",
            "output_variables": json.dumps(entry.get("m_OutputVariables") or {}),
        })
    return out


def _target_template_ids(db, ability_guid):
    # Card abilities, PvP champion powers, and PvE talent abilities all use
    # the same AbilityTemplate target contract.  Champion/talent abilities
    # are not necessarily present in card_abilities_meta.
    for table in ("card_abilities_meta", "champion_abilities",
                  "talent_abilities"):
        try:
            row = db.execute(
                "SELECT target_template_ids FROM %s "
                "WHERE ability_guid=? LIMIT 1" % table,
                (str(ability_guid).lower(),)).fetchone()
        except Exception:
            row = None
        if not row or not row[0]:
            continue
        try:
            tids = json.loads(row[0])
        except (ValueError, TypeError):
            continue
        return [str(t).lower() for t in (tids or []) if t]
    record = ability_record(db, ability_guid)
    return [str(item.get("m_Guid") or "").lower()
            for item in (record.get("m_AbilityTargetTemplateIds") or [])
            if isinstance(item, dict) and item.get("m_Guid")]


def _target_template(db, template_id):
    row = db.execute(
        "SELECT template_id, game_text, is_auto_target, is_random_target, "
        "optional, explicit, player_filter, collection_flags, "
        "min_target_count, max_target_count, filter_json, target_kind "
        "FROM target_templates WHERE template_id=?", (template_id,)).fetchone()
    if not row:
        return None
    return {
        "template_id": row[0], "game_text": row[1] or "",
        "is_auto_target": int(row[2] or 0),
        "is_random_target": int(row[3] or 0),
        "optional": int(row[4] or 0),
        "explicit": int(row[5] or 0),
        "player_filter": row[6] or "",
        "collection_flags": row[7] or "",
        "min_target_count": int(row[8] or 1),
        "max_target_count": int(row[9] or 1),
        "filter_json": row[10] or "{}",
        "target_kind": row[11] or "",
    }


def _filter_has_exact_zone(node, zone):
    """Return whether a gamedata card filter contains ``InZone(zone)``.

    Target templates commonly expose a broad ``collection_flags`` value so
    the client knows which card representations may be visible. That value
    is not the target's actual zone restriction; the nested card filter is
    authoritative. In particular, a hand target may advertise Deck as a
    known collection too.
    """
    if isinstance(node, dict):
        node_type = str(node.get("_t", "")).rsplit(".", 1)[-1]
        collection = node.get("m_Collection")
        if (node_type == "InZone" and
                str(collection or "").lower() == str(zone).lower()):
            return True
        return any(_filter_has_exact_zone(child, zone)
                   for child in node.values())
    if isinstance(node, list):
        return any(_filter_has_exact_zone(child, zone) for child in node)
    return False


def _is_deck_search_target(template):
    """Identify a target that is actually restricted to the deck.

    ``collection_flags`` is deliberately ignored here. It is a visibility
    mask and is often the all-player-collections mask, including for hand
    discard targets such as Stargazer's nested DiscardACard ability.
    """
    if not template:
        return False
    filter_json = template.get("filter_json")
    if isinstance(filter_json, str):
        try:
            filter_json = json.loads(filter_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
    return _filter_has_exact_zone(filter_json, "Deck")


def _champion_uids(handler, bstate):
    """(controller_champion_uid, opponent_champion_uid) from the handler's
    SessionCardId stubs, mirroring the client's Player.m_ChampionCard."""
    p = getattr(handler, "_player_champ_scid", None)
    a = getattr(handler, "_ai_champ_scid", None)
    pu = int(p.uid.uid64) if p is not None else None
    au = int(a.uid.uid64) if a is not None else None
    return pu, au


def _revealed_target_uids(db, session, bstate, owner_id, source_uid,
                          template):
    """Return revealed cards matching a SourceRevealed target template.

    RevealCards stores the authoritative card UIDs in battle state.  The
    target template's filter then decides which of those cards a later effect
    can select; no card-name or display-text parsing is needed.
    """
    revealed = [int(uid) for uid in ((bstate or {}).get("revealed_cards") or [])]
    if not revealed:
        return []
    filt = _parse_param(template.get("filter_json")) or {}
    out = []
    for uid in revealed:
        row = db.execute(
            "SELECT gc.card_uid, gc.card_type, gc.location, gc.user_id, "
            "gc.card_state, COALESCE(ct.attack,0), COALESCE(ct.defense,0), "
            "ct.name, COALESCE(ct.cost,0), ct.subtype, ct.threshold_json, "
            "gc.card_attributes, ct.attributes "
            "FROM game_cards gc JOIN card_templates ct "
            "ON ct.guid=gc.template_guid "
            "WHERE gc.session_id=? AND gc.card_uid=?",
            (session.session_id, uid)).fetchone()
        if not row:
            continue
        card = {
            "card_uid": int(row[0]), "card_type": row[1],
            "location": row[2], "user_id": row[3], "state": int(row[4] or 0),
            "attack": row[5], "defense": row[6], "name": row[7] or "",
            "cost": row[8] or 0, "subtype": row[9] or "",
            "shards": [], "attributes": int(row[11] or 0) | int(row[12] or 0),
            "src_owner_side": "player" if (owner_id or 0) else "ai",
        }
        if evaluate_card_filter(card, filt, source_uid):
            out.append(uid)
    return out


def _auto_target_uids(db, handler, bstate, session, ability_guid, source_uid,
                      owner_id, tidx, template, target_map=None):
    """Port of AbilityEffectInstance.ResolveAutoTarget for one target template.

    Returns (uids, resolved) — resolved=False means the template is not an auto
    target (the caller must fall back to the TargetMap / activation data).
    """
    if template is None:
        return [], False
    kind = template.get("target_kind") or ""
    player_filter = (template.get("player_filter") or "").lower()
    pu, au = _champion_uids(handler, bstate)
    if kind == "PlayerTargetTemplate":
        if (bstate or {}).get("pvp"):
            controller = pvp_champion_uid(bstate, owner_id)
            opponent_pid = pvp_opponent_pid(bstate, owner_id)
            opponent = pvp_champion_uid(bstate, opponent_pid)
            uid = (opponent if player_filter in {
                "opponent", "opposing", "singleopponent", "multipleopponents"
            } else controller)
            return ([uid] if uid is not None else []), True
        if player_filter in {"opponent", "opposing", "singleopponent",
                             "multipleopponents"}:
            uid = au if owner_id else pu
            return ([uid] if uid is not None else []), True
        # "You" / "target player": the controller's champion.
        uid = pu if owner_id else au
        return ([uid] if uid is not None else []), True
    if kind == "AbilitySourceCardTargetTemplate":
        return ([int(source_uid)] if source_uid is not None else []), True
    if kind == "AbilityTriggerCardTargetTemplate":
        # #TRIGGER_TARGET# — the trigger event's TARGET card (e.g. the
        # champion a troop damaged: "When this deals damage to an opposing
        # champion").  The trigger resolution passes it as the activation's
        # target (target_map[0]); fall back to bstate's transient target and
        # finally the source card (the old behaviour for unmapped triggers).
        for v in (target_map or {}).values():
            uids = v if isinstance(v, (list, tuple)) else [v]
            uids = [int(u) for u in uids if u is not None]
            if uids:
                return uids, True
        fb = _fallback_target_uid(bstate)
        if fb is not None:
            return [int(fb)], True
        return ([int(source_uid)] if source_uid is not None else []), True
    if kind == "AbilityCreatedTargetTemplate":
        # ``#CREATED_CARDS#`` is populated by the preceding summon/create
        # effect.  It is an AbilityInstance target list, not the source card
        # and not a generic zone query; later effects (such as Bun'jitsu's
        # stat modifiers) must target the newly-created token.
        created = ((bstate or {}).get("created_token_uids") or
                   (bstate or {}).get("created_card_uids") or [])
        return [int(uid) for uid in created if uid is not None], True
    if kind == "VoidedTargetTemplate":
        voided = ((bstate or {}).get("ability_lists") or {}).get(
            "VoidedCards")
        if voided is None:
            voided = (bstate or {}).get("champion_void_uids") or []
        return [int(uid) for uid in voided if uid is not None], True
    if kind in ("SourceRevealedTargetTemplate", "SourceDrawnTargetTemplate",
                "SourceBuriedTargetTemplate", "SourceStoredTargetTemplate"):
        # These derive from the source card's current zone/created cards; the
        # source card itself is the closest portable fallback and the leaves
        # that use them already re-resolve from bstate when given no target.
        return ([int(source_uid)] if source_uid is not None else []), True
    if not template.get("is_auto_target"):
        return [], False
    # Generic auto target: every legal card in the template's zones/filter.
    champ_pool = []
    if (bstate or {}).get("pvp"):
        for pid, cuid in ((bstate or {}).get("champ_map") or {}).items():
            try:
                pid_i = int(pid)
                cuid_i = int(cuid)
            except (TypeError, ValueError):
                continue
            health_key = ((bstate or {}).get("pvp_health_map") or {}).get(pid_i)
            champ_pool.append((
                cuid_i, pid_i, "Champion",
                int(bstate.get(health_key, 20)) if health_key else 20,
            ))
    if pu is not None:
        if not (bstate or {}).get("pvp"):
            champ_pool.append((pu, int(handler.user_profile["id"]
                                       if handler.user_profile else 0),
                               "Champion", int(bstate.get("player_health", 20))))
    if au is not None:
        if not (bstate or {}).get("pvp"):
            champ_pool.append((au, 0, "Champion",
                               int(bstate.get("ai_health", 20))))
    pool = legal_targets(db, session.session_id, owner_id,
                         template["template_id"], source_uid,
                         both_players=True, champions=champ_pool)
    if template.get("is_random_target"):
        if not pool:
            return [], True
        n = min(len(pool), max(1, template.get("max_target_count") or 1))
        return random.sample(pool, n), True
    return pool, True


def _fallback_target_uid(bstate):
    return ((bstate or {}).get("player_spell_target")
            or (bstate or {}).get("player_mod_target")
            or (bstate or {}).get("resolving_target_uid"))


def resolve_ability(handler, game, session, db, pl_t, ai_t, bstate,
                    ability_guid, source_uid, owner_id, target_map=None,
                    variables=None, depth=0, root_ability_guid=None):
    """Resolve an ability's BOM data-driven, mirroring the client's
    authoritative AbilityInstance: effects run group-by-group in order, each
    gated by its gamedata condition and contingencies, with ability variables
    carried through ActivateAbility recursion.  Returns a log string."""
    if depth > 16:
        return "resolution depth exceeded"
    bstate = bstate or {}
    supplied_variables = dict(variables or {})
    variables = ability_variables(db, ability_guid)
    variables.update(supplied_variables)
    if root_ability_guid is None:
        root_ability_guid = ability_guid
    target_map = dict(target_map or {})
    tids = _target_template_ids(db, ability_guid)
    logs = []
    # m_WasApplied per effect instance (contingencies test it), plus a
    # (ability_guid, effect_order) dedupe so duplicate rows (double-seeded
    # test DBs) can never double-fire a leaf or an ActivateAbility branch.
    applied = {}
    seen_orders = set()
    # The client stores auto-targets by target-template index on the ability
    # instance.  A random target therefore remains the same for every effect
    # that references that index (Dragon Guard Stalwart's separate +1 ATK and
    # +1 DEF leaves are one example).  Keep this cache local to one ability
    # resolution so a later trigger still gets a fresh random choice.
    random_target_cache = {}
    prev_ability = bstate.get("resolving_ability")
    prev_owner = bstate.get("resolving_owner_id")
    prev_source = bstate.get("resolving_source_uid")
    prev_effect = bstate.get("resolving_effect_guid")
    prev_grant_target = bstate.get("grant_target")
    prev_skip_transform = bstate.get("_skip_transform")
    prev_ability_damage = bstate.get("_ability_damage_dealt")
    bstate["resolving_ability"] = ability_guid
    bstate["session_id"] = session.session_id
    bstate["resolving_owner_id"] = owner_id if owner_id is not None else 0
    bstate["resolving_source_uid"] = source_uid
    bstate["_ability_damage_dealt"] = 0
    previous_variables = bstate.get("ability_variables")
    bstate["ability_variables"] = variables

    # Group the effect list by m_EffectGroupId, preserving effect order.
    groups = {}
    order = []
    for eff in _effect_list(db, ability_guid):
        gid = eff["effect_group_id"]
        if gid not in groups:
            groups[gid] = []
            order.append(gid)
        groups[gid].append(eff)

    def _condition_met(eff):
        cid = eff["condition_id"]
        if not cid:
            return True
        ctx = ConditionContext(db, session, bstate,
                               ability_source_uid=source_uid,
                               ability_source_owner_id=owner_id,
                               pl_t=pl_t, ai_t=ai_t)
        ctx.ability_variables = variables
        ctx.applied_effects = applied
        return evaluate_effect_condition(db, cid, ctx)

    def _contingency_met(eff):
        cid = eff["contingent_effect_instance_id"]
        if cid < 0:
            return True
        # The client requires the contingent effect to exist in an earlier
        # group (or an earlier instance in this group) and to have applied.
        for gid2 in order:
            for other in groups[gid2]:
                if other["effect_instance_id"] == cid:
                    if other["effect_group_id"] > eff["effect_group_id"]:
                        return False
                    if (other["effect_group_id"] == eff["effect_group_id"]
                            and other["effect_order"] > eff["effect_order"]):
                        return False
                    return bool(applied.get(cid, False))
        return False

    def _resolve_targets(eff):
        """(uids, needs_prompt) for one effect — port of HasTarget() +
        ResolveAutoTarget() + the activation TargetMap."""
        tidx = eff["target_index"]
        # RevealCards uses an AbilityTargetTemplate as a description of the
        # cards to reveal (TopNOfDeck), not as a single card target.  The leaf
        # reads the target filter's TopN value and selects the cards itself.
        if eff.get("effect_type") == "RevealCardsAbilityEffectTemplate":
            return ([int(source_uid)] if source_uid is not None else [None]), False
        if 0 <= tidx < len(tids):
            template = _target_template(db, tids[tidx])
        else:
            template = None

        def _validate_selected(values):
            if template is None or not values:
                return values
            kind = template.get("target_kind") or ""
            if kind in ("SourceRevealedTargetTemplate",
                        "SourceDrawnTargetTemplate",
                        "SourceBuriedTargetTemplate",
                        "SourceStoredTargetTemplate",
                        "VoidedTargetTemplate",
                        "AbilityCreatedTargetTemplate"):
                return values
            pool = []
            champ_fn = getattr(handler, "_champion_targets", None)
            if callable(champ_fn):
                try:
                    pool = champ_fn() or []
                except Exception:
                    pool = []
            pfilter = (template.get("player_filter") or "").lower()
            both = pfilter not in ("self", "you", "controller")
            return validate_target_selection(
                db, session.session_id, owner_id, template["template_id"],
                source_uid, values, both_players=both, champions=pool)
        # MatchSecondaryTargetTemplate is not a generic "all legal cards"
        # target.  It means every legal opposing card whose name matches the
        # target selected by the previous effect.  Countermagic relies on this
        # for its permanent +2 cost modifier across every zone.
        if (template is not None
                and (template.get("target_kind") or "")
                == "MatchSecondaryTargetTemplate"
                and eff.get("secondary_target_index", -1) >= 0):
            previous = None
            for gid2 in order:
                for other in groups[gid2]:
                    if (other["effect_instance_id"]
                            == eff["secondary_target_index"]):
                        previous = _resolve_targets(other)[0]
                        break
                if previous:
                    break
            if previous:
                target_row = db.execute(
                    "SELECT ct.name FROM game_cards gc "
                    "JOIN card_templates ct ON ct.guid=gc.template_guid "
                    "WHERE gc.session_id=? AND gc.card_uid=?",
                    (session.session_id, int(previous[0]))).fetchone()
                if target_row and target_row[0]:
                    legal = legal_targets(
                        db, session.session_id, owner_id,
                        template["template_id"], source_uid,
                        both_players=True, champions=[])
                    name_rows = db.execute(
                        "SELECT gc.card_uid FROM game_cards gc "
                        "JOIN card_templates ct ON ct.guid=gc.template_guid "
                        "WHERE gc.session_id=? AND lower(ct.name)=lower(?)",
                        (session.session_id, target_row[0])).fetchall()
                    same_name = {int(row[0]) for row in name_rows}
                    return [uid for uid in legal if int(uid) in same_name], False
        if (template is not None
                and (template.get("target_kind") or "")
                == "SourceRevealedTargetTemplate"):
            candidates = _revealed_target_uids(
                db, session, bstate, owner_id, source_uid, template)
            # A secondary SourceRevealed target means “all the other revealed
            # cards” (Oakhenge's second move effect).  Resolve the referenced
            # first target using the same metadata and remove it here.
            sti = eff.get("secondary_target_index", -1)
            if sti >= 0:
                previous = None
                for gid2 in order:
                    for other in groups[gid2]:
                        if other["effect_instance_id"] == sti:
                            previous = _resolve_targets(other)[0]
                            break
                    if previous:
                        break
                excluded = {int(uid) for uid in (previous or [])}
                candidates = [uid for uid in candidates
                              if int(uid) not in excluded]
            # A secondary SourceRevealed target is the collection left over
            # from the first selection, even though the client serializes its
            # target-template max count as one.  The first target is singular;
            # the secondary target receives every remaining revealed card.
            if sti < 0:
                max_count = max(1, int(template.get("max_target_count") or 1))
                if len(candidates) > max_count:
                    if template.get("is_random_target"):
                        candidates = random.sample(candidates, max_count)
                    else:
                        # A revealed-card target is an explicit client choice,
                        # not an auto-target.  Pause the BOM after the reveal
                        # and let the controller choose from the metadata-
                        # legal candidates.  The prompt helper owns the
                        # chooser-scoped CardsRevealed packet and persists the
                        # pending continuation.
                        prompt = getattr(handler, "_prompt_revealed_choice",
                                         None)
                        if (callable(prompt)
                                and not (bstate or {}).get(
                                    "pending_revealed_choice")):
                            prompt(game, session, pl_t, ai_t, bstate,
                                   ability_guid, int(source_uid or 0),
                                   int(owner_id or 0), candidates,
                                   list((bstate or {}).get(
                                       "revealed_cards") or []))
                            bstate["resolution_paused"] = True
                            return [], False
                        # Non-interactive/AI resolution has no client picker;
                        # choose the first legal card, matching the old harness
                        # fallback.
                        candidates = candidates[:max_count]
            return candidates, False
        if template is not None and (template.get("is_auto_target")
                                     or (template.get("target_kind") or "")
                                     in ("PlayerTargetTemplate",
                                         "AbilitySourceCardTargetTemplate",
                                         "SourceRevealedTargetTemplate",
                                         "SourceDrawnTargetTemplate",
                                         "SourceBuriedTargetTemplate",
                                         "SourceStoredTargetTemplate",
                                         "VoidedTargetTemplate",
                                         "AbilityCreatedTargetTemplate",
                                         "AbilityTriggerCardTargetTemplate")):
            cache_key = (tidx, template.get("template_id"))
            if template.get("is_random_target") and cache_key in random_target_cache:
                uids, resolved = list(random_target_cache[cache_key]), True
            else:
                uids, resolved = _auto_target_uids(
                    db, handler, bstate, session, ability_guid, source_uid,
                    owner_id, tidx, template, target_map)
                if template.get("is_random_target") and resolved:
                    random_target_cache[cache_key] = list(uids or [])
            if resolved:
                if uids:
                    return uids, False
                # Auto-resolved to nothing — keep whatever the activation map
                # already locked in (client keeps the existing TargetInstance).
        # A zone move with no target-template index is a source-card effect.
        # Do this before the root activation fallback: a spell can carry a
        # target for an earlier damage leaf while its later "put this into
        # your deck" leaf deliberately has no target template (Ragefire's
        # Escalation).  Keep the legacy target-map fallback for other rows
        # whose older extracted metadata omitted target_index.
        if (template is None and tidx < 0 and source_uid is not None
                and eff.get("effect_type") == "MoveCardToZoneEffectTemplate"):
            return [int(source_uid)], False
        if tidx in target_map:
            v = target_map[tidx]
            uids = v if isinstance(v, (list, tuple)) else [v]
            selected = [int(u) for u in uids if u is not None]
            return _validate_selected(selected), False
        # Secondary target: the target of another effect instance in THIS
        # ability (e.g. "the card targeted by effect N").
        sti = eff.get("secondary_target_index", -1)
        if sti >= 0:
            for gid2 in order:
                for other in groups[gid2]:
                    if other["effect_instance_id"] == sti:
                        prev_t = _resolve_targets(other)
                        if prev_t[0]:
                            return prev_t[0], False
        # Any other activation-map entry (single-target trees: the root
        # activation's chosen card feeds the one explicit leaf, e.g. the deck
        # search MoveCardToZone under Darkspire's Deathcry).
        for v in target_map.values():
            uids = v if isinstance(v, (list, tuple)) else [v]
            uids = [int(u) for u in uids if u is not None]
            if uids:
                return _validate_selected(uids), False
        # Activation fallback (spells / manual abilities carry their chosen
        # target in bstate) — only at the ROOT activation: children resolve
        # their own targets against their own templates.
        fb = _fallback_target_uid(bstate)
        if fb is not None and depth == 0:
            return _validate_selected([int(fb)]), False
        # Legacy default: an effect whose target template is missing/out of
        # range targets the source card (the old flat walk's _resolve_target
        # fell back to source_uid for self-buffing triggers like Righteous
        # Paladin / Incantation of Righteousness).
        if template is None and source_uid is not None:
            return [int(source_uid)], False
        # Deck search: the leaf's own target template drives a class-39
        # choosing prompt (Darkspire Priestess).  The prompt needs the ROOT
        # ability guid so _deck_search_ability can find the nested search
        # ability and its Choosing target template.
        if template is not None and _is_deck_search_target(template):
            return None, True
        return [], False

    def _prompt_or_auto_pick(eff, template):
        """Activation data for an explicit target the player must choose
        (deck search).  Human controllers get the class-39 prompt (existing
        pending_deck_search flow); the AI auto-picks a random legal card."""
        from .effects.search import move_deck_card_to_hand
        # This is a deck search whose selected card remains in the deck and
        # contributes its threshold (the Adaptable Infusion Device / Shards
        # of Fate pattern), rather than a normal search that moves a card to
        # hand.  Determine that from the target filter and BOM effect type;
        # game_text is localized display data and must not drive rules logic.
        def _metadata_has(node, type_name, field=None, value=None):
            if isinstance(node, dict):
                node_type = str(node.get("_t", "")).rsplit(".", 1)[-1]
                if node_type == type_name:
                    if field is None:
                        return True
                    actual = node.get(field)
                    if value is None or str(actual).lower() == str(value).lower():
                        return True
                return any(_metadata_has(child, type_name, field, value)
                           for child in node.values())
            if isinstance(node, list):
                return any(_metadata_has(child, type_name, field, value)
                           for child in node)
            return False

        target_filter = _parse_param(template.get("filter_json")) or {}
        has_standard_resource = (
            _metadata_has(target_filter, "IsSubType", "m_SubType", "Standard")
            and _metadata_has(target_filter, "IsResource")
            and _metadata_has(target_filter, "InZone", "m_Collection", "Deck")
        )
        has_threshold_effect = any(
            effect["effect_type"] == "TACAbilityEffectTemplate"
            for effect in _effect_list(db, ability_guid)
        )
        threshold_search = has_standard_resource and has_threshold_effect
        try:
            candidates = legal_targets(
                db, session.session_id, owner_id, template["template_id"],
                source_uid, both_players=False, champions=[])
        except Exception:
            candidates = []
        candidates = [int(c) for c in candidates]
        if not candidates:
            return "search deck: no matching card"
        if owner_id == 0:
            chosen = random.choice(candidates)
            return move_deck_card_to_hand(
                game, session, db, handler, pl_t, ai_t, chosen,
                owner_id, bstate)
        prompt = getattr(handler, "_prompt_deck_search", None)
        if callable(prompt):
            prompt_args = (game, session, pl_t, ai_t, bstate,
                           root_ability_guid, int(source_uid) if source_uid
                           else 0, int(owner_id), candidates)
            if threshold_search:
                result = prompt(*prompt_args, kind="shard")
            else:
                result = prompt(*prompt_args)
            # A human deck-search prompt is a continuation point.  The
            # ability may have more than one metadata effect referencing the
            # same target template (Adaptable Infusion Device has StoreTargets
            # followed by TAC), but the client must receive only one picker.
            if str(result).startswith("deck search: awaiting"):
                bstate["resolution_paused"] = True
            return result
        # Non-interactive handler (unit tests): auto-pick a random legal card,
        # matching the old deathcry fallback.
        chosen = random.choice(candidates)
        return move_deck_card_to_hand(
            game, session, db, handler, pl_t, ai_t, chosen, owner_id, bstate)

    for gid in order:
        for eff in groups[gid]:
            key = (ability_guid, eff["effect_order"])
            if key in seen_orders:
                continue
            seen_orders.add(key)
            inst_id = eff["effect_instance_id"]
            etype = eff["effect_type"]
            # Ability variables are set before target resolution: the
            # RandomizeVariable leaf is group 1 and the conditioned branches
            # live in later groups.
            if etype == "RandomizeVariableEffectTemplate":
                pm = _parse_param(eff["param"]) or {}
                name = pm.get("variable") or "RandomNumber"
                lo = int(pm.get("min", 1))
                hi = int(pm.get("max", lo))
                variables[name] = random.randint(lo, max(lo, hi))
                applied[inst_id] = True
                continue
            if etype in ("SetCardIntegerVariableEffectTemplate",
                         "SetAbilityVariableEffectEffectTemplate"):
                # CardIntegerVariables belong to the source card instance,
                # not to the transient AbilityInstance variable map.  Keep a
                # bstate cache for the current resolution and persist the
                # value alongside the card's other per-instance data.
                template = effect_template(eff["effect_guid"]) or {}
                pm = _parse_param(eff["param"]) or {}
                variable = (template.get("m_VariableName") or
                            pm.get("variable") or "")
                operation = (template.get("m_Operation") or
                             pm.get("operation") or "Set")
                input_field = template.get("m_InputValue")
                if input_field is not None:
                    value = resolve_field(input_field, variables,
                                          bstate.get("effect_outputs") or
                                          {}, bstate, 0)
                else:
                    value = int(pm.get("value") or 0)
                source_row = None
                if source_uid is not None:
                    source_row = db.execute(
                        "SELECT permanent_buffs FROM game_cards "
                        "WHERE session_id=? AND card_uid=?",
                        (session.session_id, int(source_uid))).fetchone()
                try:
                    instance_data = json.loads(
                        (source_row[0] if source_row else "{}") or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    instance_data = {}
                card_values = instance_data.setdefault(
                    "card_integer_variables", {})
                old_value = int(card_values.get(variable, 0) or 0)
                if str(operation).lower() == "add":
                    new_value = old_value + int(value)
                elif str(operation).lower() == "remove":
                    new_value = old_value - int(value)
                else:
                    new_value = int(value)
                if variable and source_uid is not None and source_row:
                    card_values[variable] = new_value
                    db.execute(
                        "UPDATE game_cards SET permanent_buffs=? "
                        "WHERE session_id=? AND card_uid=?",
                        (json.dumps(instance_data, separators=(",", ":")),
                         session.session_id, int(source_uid)))
                    db.commit()
                bstate.setdefault("card_integer_variables", {})[variable] = \
                    new_value
                if variable:
                    variables[variable] = new_value
                applied[inst_id] = True
                continue
            if not _condition_met(eff):
                applied[inst_id] = False
                # Incantation-style BOMs put the five-counter gate on the
                # remove-counters effect, while the following transform leaf
                # has no separate condition.  Carry the failed gate forward
                # for that transform instead of transforming the first target
                # card even though the threshold was not met.
                pm = _parse_param(eff["param"])
                if (eff["effect_type"] == "CardModifierAbilityEffectTemplate"
                        and pm and pm.get("property") == "counter"
                        and int(pm.get("amount") or 0) <= 0):
                    bstate["_skip_transform"] = True
                continue
            if not _contingency_met(eff):
                applied[inst_id] = False
                continue
            uids, needs_prompt = _resolve_targets(eff)
            if needs_prompt:
                logs.append(_prompt_or_auto_pick(eff, _target_template(
                    db, (tids[eff["target_index"]]
                         if 0 <= eff["target_index"] < len(tids) else ""))))
                applied[inst_id] = False
                if bstate.get("resolution_paused"):
                    break
                continue
            # A revealed-card prompt pauses the BOM before its leaf runs.
            # Do not fall through and execute that leaf once with a null
            # target while the client is choosing a card.
            if bstate.get("resolution_paused"):
                applied[inst_id] = False
                break
            target_template = _target_template(
                db, (tids[eff["target_index"]]
                     if 0 <= eff["target_index"] < len(tids) else ""))
            # An explicit SourceRevealed target can legitimately have no
            # legal cards (Oakhenge Ceremony when the reveal contains no
            # troops).  Treat that effect as a no-op.  Running the leaf with
            # ``None`` would make it fall back to a stale/source target and
            # incorrectly move a shard or another revealed card to hand.
            if (not uids and target_template is not None
                    and (target_template.get("target_kind") or "")
                    == "SourceRevealedTargetTemplate"):
                applied[inst_id] = True
                continue
            if etype == "ActivateAbilityEffectTemplate":
                # The client requires the ActivateAbility effect's OWN target
                # instance; each target card spawns the child with a fresh
                # target map (Session.ActivateAbilityFromEffect) so the child
                # resolves its own targets against its own templates.
                if not uids:
                    applied[inst_id] = False
                    continue
                child = (eff["param"] or "").lower()
                if not child:
                    applied[inst_id] = False
                    continue
                for t_uid in uids:
                    # The client's ActivateAbilityFromEffect passes the
                    # TARGET card's controller as the child's responsible
                    # player — "You" in the child resolves to THAT player
                    # (e.g. Spawn of Othuyeg's child "Bury the top card of
                    # your deck" buries the damaged champion's deck).
                    child_owner = owner_id
                    trow = db.execute(
                        "SELECT user_id FROM game_cards "
                        "WHERE session_id=? AND card_uid=?",
                        (session.session_id, int(t_uid))).fetchone()
                    if trow:
                        child_owner = trow[0]
                    else:
                        if (bstate or {}).get("pvp"):
                            for _pid, _cuid in (bstate.get("champ_map") or {}).items():
                                try:
                                    if int(_cuid) == int(t_uid):
                                        child_owner = int(_pid)
                                        break
                                except (TypeError, ValueError):
                                    continue
                        if not (bstate or {}).get("pvp"):
                            pu, au = _champion_uids(handler, bstate)
                            if pu is not None and int(t_uid) == int(pu):
                                child_owner = (handler.user_profile["id"]
                                               if handler.user_profile else 0)
                            elif au is not None and int(t_uid) == int(au):
                                child_owner = 0
                    logs.append(resolve_ability(
                        handler, game, session, db, pl_t, ai_t, bstate,
                        child, source_uid, child_owner, target_map,
                        variables, depth + 1, root_ability_guid))
                    if bstate.get("resolution_paused"):
                        break
                applied[inst_id] = True
                if bstate.get("resolution_paused"):
                    break
                continue
            if etype == "RepeatingAbilityEffectTemplate":
                # RepeatingAbilityEffectTemplate.Apply executes its nested
                # effect against the same AbilityEffectInstance.  The common
                # extracted form is an ActivateAbility child, so recurse
                # through the normal BOM resolver instead of interpreting the
                # display text or multiplying a leaf after the fact.
                template = effect_template(eff["effect_guid"]) or {}
                loop_count = resolve_field(
                    template.get("m_LoopCount"), variables,
                    bstate.get("effect_outputs") or {}, bstate, 0)
                nested = template.get("m_RepeatingEffect") or {}
                child = nested.get("m_AbilityToInvoke") or {}
                child_guid = str(child.get("m_Guid") or "").lower()
                if child_guid and child_guid != "0" * 36:
                    for _ in range(max(0, min(int(loop_count), 100))):
                        logs.append(resolve_ability(
                            handler, game, session, db, pl_t, ai_t, bstate,
                            child_guid, source_uid, owner_id, {}, variables,
                            depth + 1, root_ability_guid))
                        if bstate.get("resolution_paused"):
                            break
                applied[inst_id] = True
                if bstate.get("resolution_paused"):
                    break
                continue
            fn = _LEAFS.get(etype)
            if not fn:
                applied[inst_id] = True
                continue
            # A target template may resolve to multiple cards (for example
            # Countermagic's same-name cards in every opposing zone).  Apply
            # the leaf once per resolved target instead of silently using the
            # first card only.
            for target_uid in (uids or [None]):
                bstate["resolving_effect_guid"] = eff["effect_guid"]
                if target_uid is not None:
                    (bstate or {})["player_mod_target"] = target_uid
                    (bstate or {})["player_spell_target"] = target_uid
                    (bstate or {})["resolving_target_uid"] = target_uid
                    if etype == "GrantAbilityEffectTemplate":
                        # GrantAbility applies to the effect's resolved target
                        # (normally the source card).  Keep this explicit so a
                        # granted trigger survives a zone transfer such as
                        # Reginald moving into the opponent's deck.
                        bstate["grant_target"] = target_uid
                logs.append(fn(game, session, db, handler, pl_t, ai_t, bstate,
                               eff["effect_guid"], eff["param"]))
            applied[inst_id] = True
            if bstate.get("resolution_paused"):
                break
        if bstate.get("resolution_paused"):
            break

    if prev_ability is None:
        bstate.pop("resolving_ability", None)
    else:
        bstate["resolving_ability"] = prev_ability
    if prev_owner is None:
        bstate.pop("resolving_owner_id", None)
    else:
        bstate["resolving_owner_id"] = prev_owner
    if prev_source is None:
        bstate.pop("resolving_source_uid", None)
    else:
        bstate["resolving_source_uid"] = prev_source
    if previous_variables is None:
        bstate.pop("ability_variables", None)
    else:
        bstate["ability_variables"] = previous_variables
    if prev_effect is None:
        bstate.pop("resolving_effect_guid", None)
    else:
        bstate["resolving_effect_guid"] = prev_effect
    if prev_grant_target is None:
        bstate.pop("grant_target", None)
    else:
        bstate["grant_target"] = prev_grant_target
    if prev_skip_transform is None:
        bstate.pop("_skip_transform", None)
    else:
        bstate["_skip_transform"] = prev_skip_transform
    if prev_ability_damage is None:
        bstate.pop("_ability_damage_dealt", None)
    else:
        bstate["_ability_damage_dealt"] = prev_ability_damage
    return "; ".join(str(l) for l in logs if l)
