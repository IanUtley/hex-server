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
from .fields import (ability_variables, effect_template,
                     effect_template_value, resolve_field)
from .targeting import (legal_targets, evaluate_card_filter,
                         validate_target_selection)
from ._shared import pvp_champion_uid, pvp_opponent_pid
from .builder import AbilityBuilder, AbilityContinuation
from .context import EffectContext
from .trace import begin_effect, end_effect
from gamedata import DEFAULT_RECORD_STORE, ability_graph, runtime_effects
from gamedata.play_plan import ActivationData


_RECORD_STORE = DEFAULT_RECORD_STORE


def _randint(bstate, minimum, maximum):
    """Use the migrated session RNG, falling back for legacy callers."""
    rng = (bstate or {}).get("_rules_rng")
    if rng is not None and hasattr(rng, "next_range"):
        lo, hi = int(minimum), int(maximum)
        return lo if hi <= lo else int(rng.next_range(lo, hi + 1))
    return random.randint(int(minimum), int(maximum))


def _choice(bstate, values):
    values = list(values)
    if not values:
        raise IndexError("cannot choose from an empty sequence")
    rng = (bstate or {}).get("_rules_rng")
    if rng is not None and hasattr(rng, "next"):
        return values[int(rng.next(len(values)))]
    return random.choice(values)


def _sample(bstate, values, count):
    pool = list(values)
    count = max(0, min(int(count), len(pool)))
    if count == 0:
        return []
    rng = (bstate or {}).get("_rules_rng")
    if rng is None or not hasattr(rng, "next"):
        return random.sample(pool, count)
    result = []
    for _ in range(count):
        result.append(pool.pop(int(rng.next(len(pool)))))
    return result


def _parse_param(param):
    if not param:
        return None
    # Current Records-backed TargetSpec data is already materialized as a
    # dict.  Keep accepting the legacy JSON string form, but do not discard
    # typed filters before inspecting them (the built-in ChooseAndPlay target
    # is the important case).
    if isinstance(param, (dict, list)):
        return param
    try:
        d = json.loads(param)
        return d if isinstance(d, dict) else None
    except (ValueError, TypeError):
        return None


def _effect_list(db, ability_guid):
    graph = ability_graph(_RECORD_STORE, str(ability_guid).lower())
    if graph is None:
        raise RuntimeError(
            f"ability {str(ability_guid).lower()} is missing from current Records")
    return list(runtime_effects(graph))


def _target_template_ids(db, ability_guid):
    graph = ability_graph(_RECORD_STORE, str(ability_guid).lower())
    if graph is None:
        raise RuntimeError(
            f"ability {str(ability_guid).lower()} is missing from current Records")
    return [target.guid for target in graph.targets]


def _target_template(db, template_id):
    from pvp_db import db_target_template_row
    row = db_target_template_row(template_id, conn=db)
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


def _target_template_from_spec(spec):
    """Adapt one current Records TargetSpec for the targeting ABI."""
    if spec is None:
        return None
    card_filter = spec.card_filter
    if hasattr(card_filter, "to_dict"):
        card_filter = card_filter.to_dict()
    return {
        "template_id": spec.guid,
        "game_text": spec.name or "",
        "is_auto_target": int(spec.is_auto),
        "is_random_target": int(spec.is_random),
        "optional": int(spec.optional),
        "explicit": int(spec.explicit),
        "player_filter": spec.player_filter or "",
        "collection_flags": spec.collection_flags or "",
        "min_target_count": int(spec.minimum or 0),
        "max_target_count": int(spec.maximum or 0),
        "filter_json": card_filter or {},
        "target_kind": spec.target_kind or "",
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


def _champion_targets(handler, bstate):
    """Return live champion cards for condition evaluation.

    Most PvE handlers expose ``_champion_targets`` directly.  PvP resolves
    against a FRA-shaped view instead, so construct the same tuples from its
    persisted ``champ_map`` and health mapping when needed.
    """
    provider = getattr(handler, "_champion_targets", None)
    if callable(provider) and not (bstate or {}).get("pvp"):
        try:
            return provider() or []
        except Exception:
            pass
    if (bstate or {}).get("pvp"):
        result = []
        health_map = (bstate or {}).get("pvp_health_map") or {}
        for pid, cuid in ((bstate or {}).get("champ_map") or {}).items():
            try:
                pid_i, cuid_i = int(pid), int(cuid)
            except (TypeError, ValueError):
                continue
            key = health_map.get(pid_i)
            if key is None:
                key = health_map.get(str(pid_i))
            if key:
                hp = int(bstate.get(key, 20) or 0)
            else:
                hp = int(bstate.get(f"hp_{pid_i}", 20) or 0)
            result.append((cuid_i, pid_i, "Champion", hp))
        return result
    pu, au = _champion_uids(handler, bstate)
    result = []
    if pu is not None:
        profile = getattr(handler, "user_profile", None)
        owner = profile.get("id", 0) if isinstance(profile, dict) else 0
        result.append((pu, owner, "Player", int(
            bstate.get("player_health", 20) or 0)))
    if au is not None:
        result.append((au, 0, "AI", int(
            bstate.get("ai_health", 20) or 0)))
    return result


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
        from pvp_db import db_condition_card_row
        full_row = db_condition_card_row(session.session_id, uid, conn=db)
        row = (full_row[:7] + (full_row[8], full_row[9], full_row[10],
                               full_row[11], full_row[12], full_row[13])
               if full_row else None)
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
            # Lightweight/PvP-shaped headless sessions may expose champion
            # SessionCardIds on the handler before they have constructed the
            # persisted champ_map.  Use those IDs only as a fallback; live
            # sessions remain authoritative through champ_map above.
            if controller is None:
                player_champ, ai_champ = _champion_uids(handler, bstate)
                player_id = getattr(handler, "user_profile", {}) or {}
                player_id = player_id.get("id")
                if player_id is not None and int(owner_id) == int(player_id):
                    controller, opponent = player_champ, ai_champ
                else:
                    controller, opponent = ai_champ, player_champ
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
        # Card-scoped void relationships are recorded by the void leaf under
        # the resolving source card.  This is the generic client meaning of
        # VoidedTargetTemplate for abilities such as "put each card voided by
        # it into play"; it is not limited to champion void effects.
        if not voided:
            source_voided = ((bstate or {}).get("voided_by") or {}).get(
                str(int(source_uid))) if source_uid is not None else None
            voided = source_voided or []
        return [int(uid) for uid in voided if uid is not None], True
    if kind in ("SourceRevealedTargetTemplate", "SourceDrawnTargetTemplate",
                "SourceBuriedTargetTemplate", "SourceStoredTargetTemplate"):
        # These derive from the source card's current zone/created cards; the
        # source card itself is the closest portable fallback and the leaves
        # that use them already re-resolve from bstate when given no target.
        return ([int(source_uid)] if source_uid is not None else []), True
    # Random target templates are resolved by the client without an explicit
    # selection even when the serialized template is not marked AutoTarget.
    # Treating them as activation prompts left random discard/void/exhaust
    # effects with no target in the server resolver.
    if not template.get("is_auto_target") and not template.get("is_random_target"):
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
                         both_players=True, champions=champ_pool,
                         battle_state=bstate)
    if template.get("is_random_target"):
        if not pool:
            return [], True
        n = min(len(pool), max(1, template.get("max_target_count") or 1))
        return _sample(bstate, pool, n), True
    return pool, True


def _fallback_target_uid(bstate):
    return ((bstate or {}).get("player_spell_target")
            or (bstate or {}).get("player_mod_target")
            or (bstate or {}).get("resolving_target_uid"))


def resolve_ability(handler, game, session, db, pl_t, ai_t, bstate,
                    ability_guid, source_uid, owner_id, target_map=None,
                    variables=None, depth=0, root_ability_guid=None,
                    resume_from_order=None, activation_data=None,
                    effect_groups=None, native_effect=None):
    """Resolve an ability's BOM data-driven, mirroring the client's
    authoritative AbilityInstance: effects run group-by-group in order, each
    gated by its gamedata condition and contingencies, with ability variables
    carried through ActivateAbility recursion.  Returns a log string."""
    if depth > 16:
        return "resolution depth exceeded"
    bstate = bstate or {}
    # A live RulesPort session may use this module only as the explicit
    # Records effect interpreter.  Direct callers otherwise create a second
    # ability lifecycle (old target/continuation/stack semantics) alongside
    # the port.  Fail at the boundary so new gameplay paths cannot silently
    # reintroduce the hybrid architecture.
    if ((getattr(session, "_rules_port_session", None) is not None or
         bstate.get("_rules_port_attached")) and
            not bstate.get("_rules_port_allow_legacy_backend")):
        raise RuntimeError(
            "legacy ability resolver bypassed RulesPort; use "
            "rules_port.resolve_port_ability")
    incoming_activation = (ActivationData.from_dict(activation_data)
                           if activation_data is not None else None)
    if incoming_activation is not None:
        if target_map is None:
            target_map = incoming_activation.target_map
        supplied_variables = dict(incoming_activation.variables)
        supplied_variables.update(variables or {})
    else:
        supplied_variables = dict(variables or {})
    graph = ability_graph(_RECORD_STORE, str(ability_guid).lower())
    if graph is not None:
        variables = {
            str(variable.field("m_Name", "")): int(
                variable.field("m_DefaultValue", 0) or 0)
            for variable in graph.variables
            if variable.field("m_Name")
        }
    else:
        # Synthetic unit tests may replace the two runtime loaders with an
        # explicit fixture adapter. Production abilities are Records-backed
        # and fail below when no graph is available.
        variables = ability_variables(db, ability_guid)
    variables.update(supplied_variables)
    if root_ability_guid is None:
        root_ability_guid = ability_guid
    target_map = dict(target_map or {})
    if graph is not None:
        tids = [target.guid for target in graph.targets]
        effect_rows = list(runtime_effects(graph))
    else:
        tids = _target_template_ids(db, ability_guid)
        effect_rows = _effect_list(db, ability_guid)
    activation = incoming_activation or ActivationData.from_values(
        target_map=target_map, variables=supplied_variables)
    if graph is not None:
        ability_builder = AbilityBuilder.from_graph(
            graph, source_uid=source_uid, owner_id=owner_id,
            activation=activation, store=_RECORD_STORE)
    else:
        # Explicit test adapters only; live resolution never reaches this
        # branch because the current Records graph is required.
        ability_builder = AbilityBuilder.from_runtime(
            ability_guid, effect_rows, len(tids), source_uid=source_uid,
            owner_id=owner_id, activation=activation)
    target_map = dict(activation.target_map)
    allowed_effect_groups = None
    if effect_groups is not None:
        allowed_effect_groups = {int(group) for group in effect_groups}
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
    prev_effect_order = bstate.get("resolving_effect_order")
    previous_target_map = bstate.get("ability_target_map")
    prev_grant_target = bstate.get("grant_target")
    prev_skip_transform = bstate.get("_skip_transform")
    prev_ability_damage = bstate.get("_ability_damage_dealt")
    previous_ability_lists = bstate.get("ability_lists")
    if isinstance(previous_ability_lists, dict):
        previous_ability_lists = dict(previous_ability_lists)
    bstate["resolving_ability"] = ability_guid
    bstate["session_id"] = session.session_id
    bstate["resolving_owner_id"] = owner_id if owner_id is not None else 0
    bstate["resolving_source_uid"] = source_uid
    bstate["_ability_damage_dealt"] = 0
    previous_variables = bstate.get("ability_variables")
    bstate["ability_variables"] = variables
    bstate["ability_target_map"] = dict(target_map)

    # Group the effect list by m_EffectGroupId, preserving effect order.
    groups = {}
    order = []
    for eff in ability_builder.effects:
        gid = eff["effect_group_id"]
        if gid not in groups:
            groups[gid] = []
            order.append(gid)
        groups[gid].append(eff)

    def _target_at(index):
        if graph is not None and 0 <= index < len(ability_builder.instance.targets):
            return _target_template_from_spec(ability_builder.target(index).spec)
        return _target_template(
            db, tids[index] if 0 <= index < len(tids) else "")

    def _condition_met(eff):
        cid = eff["condition_id"]
        if not cid:
            return True
        ctx = ConditionContext(db, session, bstate,
                               ability_source_uid=source_uid,
                               ability_source_owner_id=owner_id,
                               pl_t=pl_t, ai_t=ai_t,
                               champions=_champion_targets(handler, bstate))
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
            # A nested/metadata-only activation can have no source card.  An
            # absent source is an empty target set, never a list containing
            # ``None``: secondary-target resolution treats a non-empty list as
            # a real target and would otherwise attempt ``int(None)``.
            return ([int(source_uid)] if source_uid is not None else []), False
        template = _target_at(tidx)

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
        # MatchSecondaryTargetTemplate is used for two related metadata
        # contracts.  Countermagic matches every card with the same name as a
        # previously selected card; Withering Touch uses the same template
        # shape to mean every legal card controlled by the previously selected
        # champion.  Keep both meanings data-driven: a champion has no
        # game_cards row, so the old name-only lookup made Withering's hand
        # selector empty and accidentally hid pure artifacts as well.
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
                from pvp_db import (db_condition_card_row,
                                    db_session_card_owners, db_cards_with_name)
                previous_projection = db_condition_card_row(
                    session.session_id, int(previous[0]), conn=db)
                target_row = ((previous_projection[8],)
                              if previous_projection else None)
                champ_pool = _champion_targets(handler, bstate)
                previous_owner = None
                if target_row is None:
                    for champ_uid, champ_owner, _champ_name, _champ_hp in champ_pool:
                        if int(champ_uid) == int(previous[0]):
                            previous_owner = int(champ_owner)
                            break
                if previous_owner is not None:
                    # The target template still supplies the card-type and
                    # zone predicates.  Filter the legal result to the
                    # controller of the previous champion; using
                    # both_players=False here would incorrectly apply the
                    # template's ``MultiplePlayers`` opposing predicate to
                    # the same controller and return nothing.
                    legal = legal_targets(
                        db, session.session_id, owner_id,
                        template["template_id"], source_uid,
                        both_players=True, champions=champ_pool,
                        battle_state=bstate)
                    owners = {int(row[0]): int(row[1]) for row in
                              db_session_card_owners(session.session_id, conn=db)}
                    return [uid for uid in legal
                            if owners.get(int(uid)) == previous_owner], False
                if target_row and target_row[0]:
                    legal = legal_targets(
                        db, session.session_id, owner_id,
                        template["template_id"], source_uid,
                        both_players=True, champions=champ_pool,
                        battle_state=bstate)
                    name_rows = db_cards_with_name(
                        session.session_id, target_row[0], conn=db)
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
                # SourceRevealed targets are normally selected by the client,
                # even when the reveal produced exactly one legal card.  The
                # old ``len > max`` check accidentally auto-selected that card
                # and skipped optional pickers such as Starsphere's
                # "optional revealed card" child ability.
                optional_revealed = (
                    bool(template.get("optional"))
                    or int(template.get("min_target_count") or 0) == 0
                )
                should_prompt = bool(candidates) and (
                    optional_revealed or len(candidates) > max_count)
                if should_prompt:
                    if template.get("is_random_target"):
                        candidates = _sample(bstate, candidates, max_count)
                    else:
                        # A revealed-card target is an explicit client choice,
                        # not an auto-target.  Pause the BOM after the reveal
                        # and let the controller choose from the metadata-
                        # legal candidates.  The prompt helper owns the
                        # chooser-scoped CardsRevealed packet and persists the
                        # pending continuation.
                        prompt = getattr(handler, "_prompt_revealed_choice",
                                         None)
                        if (int(owner_id or 0) != 0 and callable(prompt)
                                and not (bstate or {}).get(
                                    "pending_revealed_choice")):
                            prompt(game, session, pl_t, ai_t, bstate,
                                   ability_guid, int(source_uid or 0),
                                   int(owner_id or 0), candidates,
                                   list((bstate or {}).get(
                                       "revealed_cards") or []),
                                   optional=optional_revealed)
                            bstate["resolution_paused"] = True
                            return [], False
                        # AI-controlled revealed-card choices are random in
                        # the client rules engine; never open the human card
                        # picker for an AI Oakhenge-style effect.
                        if int(owner_id or 0) == 0:
                            import random as _random
                            candidates = [_random.choice(candidates)]
                        else:
                            candidates = candidates[:max_count]
            return candidates, False
        if template is not None and (template.get("is_auto_target")
                                     or template.get("is_random_target")
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
        # A Choice-card target is an explicit built-in ChooseAndPlay picker.
        # The generated cards are real instances in Choosing; expose those
        # instances through the same PlayerOptionList used by DoubleChoice.
        # ``_choice_parent`` carries the enclosing BOM continuation because
        # the child PlayCard ability is entered through ActivateAbility.
        if (template is not None and
                _filter_has_exact_zone(_parse_param(
                    template.get("filter_json")), "Choosing")):
            candidates = legal_targets(
                db, session.session_id, owner_id, template["template_id"],
                source_uid, both_players=False)
            candidates = [int(uid) for uid in candidates]
            if candidates:
                # The client AI resolves built-in choice-card pickers without
                # opening the human chooser. Select only from the generated,
                # metadata-legal instances; this prevents stale/foreign
                # choice cards from being treated as an option.
                if int(owner_id or 0) == 0:
                    from .effects.choices import ai_choice_prefer_missing
                    return [ai_choice_prefer_missing(
                        db, session, bstate, candidates)], False
                if not (bstate or {}).get("pending_choice"):
                    parent = (bstate or {}).get("_choice_parent") or {}
                    pending = AbilityContinuation.from_state(
                        bstate,
                        ability_guid=(parent.get("ability_guid") or
                                      ability_guid),
                        source_uid=source_uid, owner_id=owner_id,
                        target_map=target_map, variables=variables,
                        resume_effect_order=int(parent.get(
                            "resume_effect_order",
                            int(eff["effect_order"]) + 1)),
                    ).to_dict()
                    pending.update({"kind": "choice_card_target",
                                    "choice_uids": candidates})
                    prompt = getattr(handler, "_prompt_choice_cards", None)
                    if callable(prompt):
                        prompt(game, session, pl_t, ai_t, bstate, pending)
                        bstate["resolution_paused"] = True
                        return [], False
        # A nested DiscardCard ability (for example Stargazer's
        # ``DiscardACard`` child) owns an explicit hand target.  It is a
        # continuation checkpoint, not an auto-target: publish the normal
        # class-23 configuration through the host and resume the parent BOM
        # with the selected target map on the following transaction.
        if (template is not None and
                eff.get("effect_type") == "DiscardCardAbilityEffectTemplate"
                and "hand" in str(template.get("collection_flags", "")).lower()
                and not (bstate or {}).get("pending_discard_ability")):
            prompt = getattr(handler, "_push_discard_prompt", None)
            if callable(prompt):
                bstate["rules_port_resume_effect_order"] = int(
                    eff.get("effect_order", 0)) + 1
                prompt(game, session, pl_t, ai_t, bstate,
                       ability_guid=ability_guid)
                if "ai_discarded_uid" not in bstate:
                    bstate["resolution_paused"] = True
                return [], False
        # ``PutThisIntoYourDeck`` is authored as a source-card move even when
        # the extracted target template describes the returned card instead
        # of carrying an explicit selection.  Preserve the C# source binding
        # rather than allowing an unrelated activation target to redirect it.
        if (source_uid is not None and
                eff.get("effect_type") == "MoveCardToZoneEffectTemplate"):
            move_param = _parse_param(eff.get("param")) or {}
            move_name = str(move_param.get("name", "")).lower()
            move_dest = str(move_param.get("destination", "")).lower()
            if move_name == "putthisintoyourdeck" or move_dest.endswith("deck"):
                return [int(source_uid)], False
        # A zone move with no target-template index is a source-card effect.
        # Do this before the root activation fallback: a spell can carry a
        # target for an earlier damage leaf while its later "put this into
        # your deck" leaf deliberately has no target template (Ragefire's
        # Escalation).  Keep the legacy target-map fallback for other rows
        # whose older extracted metadata omitted target_index.
        if (template is None and tidx < 0 and source_uid is not None
                and eff.get("effect_type") == "MoveCardToZoneEffectTemplate"):
            return [int(source_uid)], False
        # A few focused/data-light databases retain the typed destination and
        # effect row but omit the referenced source-card target template. The
        # client still treats "put this into your deck" as a source-card move;
        # do not let an unrelated activation target redirect that move.
        if (template is None and source_uid is not None
                and eff.get("effect_type") == "MoveCardToZoneEffectTemplate"):
            typed_dest = effect_template_value(
                db, bstate, eff["effect_guid"], "m_DestinationCollection", "")
            param = _parse_param(eff.get("param")) or {}
            destination = str(typed_dest or param.get("destination") or "")
            if destination.rsplit(".", 1)[-1].lower() == "deck":
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
        # Some abilities use the same client target picker to choose a card
        # that remains in its zone.  Scheme is the important example: its
        # following typed effect creates four matching cards in the deck.
        # This must be identified from the BOM, not card text or a card GUID.
        matching_target_effect = None
        for effect in _effect_list(db, ability_guid):
            if effect["effect_type"] != (
                    "CreateTokenMatchingTargetAbilityEffectTemplate"):
                continue
            effect_template_row = effect_template(effect["effect_guid"]) or {}
            collection = effect_template_row.get("m_CardCollection")
            if str(collection).rsplit(".", 1)[-1].lower() == "deck":
                matching_target_effect = effect
                break
        matching_target = matching_target_effect is not None
        try:
            candidates = legal_targets(
                db, session.session_id, owner_id, template["template_id"],
                source_uid, both_players=False, champions=[])
        except Exception:
            candidates = []
        candidates = [int(c) for c in candidates]
        if not candidates:
            return "search deck: no matching card"
        # AI/non-interactive resolution still uses the same target semantics:
        # choose a legal card, keep it in the deck, and let the following
        # typed matching-token effect run against that target.  Do not route
        # this through the ordinary search-to-hand helper.
        if matching_target and (owner_id == 0 or
                                not callable(getattr(
                                    handler, "_prompt_deck_search", None))):
            chosen = _choice(bstate, candidates)
            target_map[int(eff["target_index"])] = int(chosen)
            return f"matching target: selected {hex(int(chosen))}"
        if owner_id == 0:
            chosen = _choice(bstate, candidates)
            return move_deck_card_to_hand(
                game, session, db, handler, pl_t, ai_t, chosen,
                owner_id, bstate)
        prompt = getattr(handler, "_prompt_deck_search", None)
        if callable(prompt):
            prompt_args = (game, session, pl_t, ai_t, bstate,
                           root_ability_guid, int(source_uid) if source_uid
                           else 0, int(owner_id), candidates)
            if matching_target:
                parent = dict((bstate or {}).get("_choice_parent") or {})
                continuation = {
                    "ability_guid": str(ability_guid).lower(),
                    "source_uid": (int(source_uid)
                                   if source_uid is not None else 0),
                    "owner_id": int(owner_id),
                    "target_index": int(eff["target_index"]),
                    "target_map": {
                        str(key): value for key, value in target_map.items()
                    },
                    "variables": dict(variables or {}),
                    "parent": parent,
                }
                result = prompt(*prompt_args, kind="matching_target",
                                continuation=continuation)
            elif threshold_search:
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
        chosen = _choice(bstate, candidates)
        return move_deck_card_to_hand(
            game, session, db, handler, pl_t, ai_t, chosen, owner_id, bstate)

    for gid in order:
        for eff in groups[gid]:
            if (allowed_effect_groups is not None and
                    int(eff["effect_group_id"]) not in allowed_effect_groups):
                continue
            key = (ability_guid, eff["effect_order"])
            if key in seen_orders:
                continue
            seen_orders.add(key)
            inst_id = eff["effect_instance_id"]
            etype = eff["effect_type"]
            # A human choice can suspend the middle of a BOM.  The
            # continuation re-enters this resolver with the next effect
            # order, so already-completed groups are skipped without replaying
            # their mutations or re-rolling random values.
            if (resume_from_order is not None and
                    int(eff["effect_order"]) < int(resume_from_order)):
                applied[inst_id] = True
                continue
            # Ability variables are set before target resolution: the
            # RandomizeVariable leaf is group 1 and the conditioned branches
            # live in later groups.
            if etype == "RandomizeVariableEffectTemplate":
                pm = _parse_param(eff["param"]) or {}
                name = pm.get("variable") or "RandomNumber"
                lo = int(pm.get("min", 1))
                hi = int(pm.get("max", lo))
                variables[name] = _randint(bstate, lo, max(lo, hi))
                applied[inst_id] = True
                continue
            if etype in ("SetCardIntegerVariableEffectTemplate",
                         "SetConstantValueVariableEffectTemplate",
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
                if etype == "SetConstantValueVariableEffectTemplate":
                    # The deprecated constant template stores its operand in
                    # m_Value rather than the CardAbility InputValue field.
                    input_field = template.get("m_Value", pm.get("value", 0))
                if input_field is not None and not isinstance(input_field, (int, float)):
                    value = resolve_field(input_field, variables,
                                          bstate.get("effect_outputs") or
                                          {}, bstate, 0)
                else:
                    value = int(input_field if input_field is not None
                                else (pm.get("value") or 0))
                source_row = None
                if source_uid is not None:
                    from pvp_db import db_card_mutation_field
                    source_buffs = db_card_mutation_field(
                        session.session_id, int(source_uid),
                        "permanent_buffs", conn=db)
                    source_row = (source_buffs,) if source_buffs is not None else None
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
                    from pvp_db import db_set_card_mutation_field
                    db_set_card_mutation_field(
                        session.session_id, int(source_uid), "permanent_buffs",
                        json.dumps(instance_data, separators=(",", ":")), conn=db)
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
            if bstate.pop("ai_discarded_uid", "__missing__") != "__missing__":
                # The AI continuation already performed this discard through
                # the owner-aware mutation path; do not execute the child
                # discard leaf a second time or open a player picker.
                applied[inst_id] = True
                continue
            uids, needs_prompt = _resolve_targets(eff)
            if needs_prompt:
                logs.append(_prompt_or_auto_pick(
                    eff, _target_at(eff["target_index"])))
                applied[inst_id] = False
                if bstate.get("resolution_paused"):
                    break
                continue
            if "ai_discarded_uid" in bstate:
                bstate.pop("ai_discarded_uid", None)
                applied[inst_id] = True
                continue
            # A revealed-card prompt pauses the BOM before its leaf runs.
            # Do not fall through and execute that leaf once with a null
            # target while the client is choosing a card.
            if bstate.get("resolution_paused"):
                applied[inst_id] = False
                break
            target_template = _target_at(eff["target_index"])
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
            # A target-template effect with no resolved cards is a no-op. Do
            # not pass None to a leaf: parameterless leaves traditionally use
            # the source as their fallback, but an optional target such as a
            # Deploy sacrifice must not sacrifice its own source by accident.
            if not uids and target_template is not None:
                applied[inst_id] = False
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
                if not child or child == "00000000-0000-0000-0000-000000000000":
                    applied[inst_id] = False
                    continue
                for t_uid in uids:
                    # The client's ActivateAbilityFromEffect passes the
                    # TARGET card's controller as the child's responsible
                    # player — "You" in the child resolves to THAT player
                    # (e.g. Spawn of Othuyeg's child "Bury the top card of
                    # your deck" buries the damaged champion's deck).
                    child_owner = owner_id
                    from pvp_db import db_card_owner_id
                    target_owner = db_card_owner_id(
                        session.session_id, int(t_uid), conn=db)
                    if target_owner is not None:
                        child_owner = target_owner
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
                    previous_choice_parent = bstate.get("_choice_parent")
                    bstate["_choice_parent"] = AbilityContinuation.from_state(
                        bstate, ability_guid=ability_guid,
                        source_uid=source_uid, owner_id=owner_id,
                        target_map=target_map, variables=variables,
                        resume_effect_order=int(
                            eff["effect_order"]) + 1).to_dict()
                    try:
                        logs.append(resolve_ability(
                            handler, game, session, db, pl_t, ai_t, bstate,
                            child, source_uid, child_owner, target_map,
                            variables, depth + 1, root_ability_guid,
                            native_effect=native_effect))
                    finally:
                        if previous_choice_parent is None:
                            bstate.pop("_choice_parent", None)
                        else:
                            bstate["_choice_parent"] = previous_choice_parent
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
                            depth + 1, root_ability_guid,
                            native_effect=native_effect))
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
            # SecondaryTargetIndex refers to the earlier effect instance's
            # resolved target, not to another activation target. Keep that
            # typed relationship available to leaves such as BlockEffect.
            secondary_uid = None
            secondary_index = eff.get("secondary_target_index", -1)
            if secondary_index >= 0:
                for gid2 in order:
                    for other in groups[gid2]:
                        if other["effect_instance_id"] != secondary_index:
                            continue
                        previous_uids, _previous_prompt = _resolve_targets(other)
                        previous_uids = [uid for uid in (previous_uids or [])
                                         if uid is not None]
                        if previous_uids:
                            secondary_uid = int(previous_uids[0])
                        break
                    if secondary_uid is not None:
                        break
            previous_secondary_uid = bstate.get(
                "resolving_secondary_target_uid")
            if secondary_uid is None:
                bstate.pop("resolving_secondary_target_uid", None)
            else:
                bstate["resolving_secondary_target_uid"] = secondary_uid
            # A target template may resolve to multiple cards (for example
            # Countermagic's same-name cards in every opposing zone).  Apply
            # the leaf once per resolved target instead of silently using the
            # first card only.
            for target_uid in (uids or [None]):
                bstate["resolving_effect_guid"] = eff["effect_guid"]
                bstate["resolving_effect_order"] = eff["effect_order"]
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
                else:
                    # Target aliases are per-effect execution state, not
                    # persistent ability state.  Leaving the previous
                    # effect's target here lets source-bound effects such as
                    # Tunnel/MoveCardToZone act on an unrelated card (often
                    # the player's card when an opponent effect resolves).
                    # Source-bound leaves use resolving_source_uid as their
                    # explicit fallback, so stale aliases must be removed.
                    for key in ("player_mod_target", "player_spell_target",
                                "resolving_target_uid", "grant_target"):
                        bstate.pop(key, None)
                trace = begin_effect(db, session, game, bstate, eff, target_uid)
                previous_native_dispatch = bstate.get(
                    "_rules_port_native_effect")
                if native_effect is not None:
                    bstate["_rules_port_native_effect"] = True
                context_factory = (EffectContext.from_rules_port
                                   if (bstate or {}).get(
                                       "_rules_port_attached") else
                                   EffectContext.from_legacy)
                context = context_factory(
                    game, session, db, handler, pl_t, ai_t, bstate,
                    eff["effect_guid"], eff["param"],
                    ability=ability_builder)
                try:
                    result = (native_effect(etype, context, eff)
                              if native_effect is not None else None)
                    if result is None:
                        if native_effect is not None:
                            # Keep the compatibility boundary observable.
                            # A native resolver must never silently turn an
                            # unported effect into a successful port effect.
                            bstate.setdefault(
                                "rules_port_legacy_effects", []).append(etype)
                            if bstate.get("_rules_port_strict_effects"):
                                raise RuntimeError(
                                    "RulesPort effect has no native handler: "
                                    f"{etype} ({eff.get('effect_guid')})")
                        result = fn(context)
                except Exception as exc:
                    end_effect(db, session, game, bstate, trace, error=exc)
                    raise
                finally:
                    if previous_native_dispatch is None:
                        bstate.pop("_rules_port_native_effect", None)
                    else:
                        bstate["_rules_port_native_effect"] = previous_native_dispatch
                end_effect(db, session, game, bstate, trace, result=result)
                logs.append(result)
            if previous_secondary_uid is None:
                bstate.pop("resolving_secondary_target_uid", None)
            else:
                bstate["resolving_secondary_target_uid"] = previous_secondary_uid
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
    if prev_effect_order is None:
        bstate.pop("resolving_effect_order", None)
    else:
        bstate["resolving_effect_order"] = prev_effect_order
    if previous_target_map is None:
        bstate.pop("ability_target_map", None)
    else:
        bstate["ability_target_map"] = previous_target_map
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
    if previous_ability_lists is None:
        bstate.pop("ability_lists", None)
    else:
        bstate["ability_lists"] = previous_ability_lists
    return "; ".join(str(l) for l in logs if l)
