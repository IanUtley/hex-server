"""RulesPort-owned ability resolution boundary."""

from __future__ import annotations

import json

from .actions import AbilityResolutionState


def _log_ability_start(handler, db, ability_guid, source_uid, owner_id,
                       target_map):
    """Write one readable trace line for every RulesPort ability resolution."""
    try:
        from pvp_db import db_ability_game_text, db_ability_raw_json
        import json
        raw = db_ability_raw_json(ability_guid, conn=db)
        record = json.loads(raw or "{}") if raw else {}
        name = record.get("m_Name") or db_ability_game_text(
            ability_guid, conn=db) or str(ability_guid)
    except Exception:
        name = str(ability_guid)
    targets = []
    for value in (target_map or {}).values():
        if isinstance(value, dict):
            value = value.get("value", value.get("uid64", value))
        try:
            targets.append(hex(int(value)))
        except (TypeError, ValueError):
            targets.append(str(value))
    getattr(handler, "_log_req", print)(
        f"    Ability resolve: {name} ({str(ability_guid)[:8]}) "
        f"source={hex(int(source_uid)) if source_uid is not None else None} "
        f"owner={owner_id} targets={targets}")


def _random_target_sample(candidates, count, battle_state):
    """Port of ``AbilityTargetTemplate.FilterRandomTargets``.

    A random auto-target resolves from the full legal pool but the effect
    applies to at most ``count`` cards chosen with the session RNG.  ``count``
    of 0 or less means "unlimited" (C# ``GetMaximumTargetCount`` returns
    ``int.MaxValue`` when unset).  The C# client does a partial Fisher-Yates:
    for ``i`` from ``n`` down to ``n - count + 1`` it picks ``rng.Next(i)``,
    swaps that slot with slot ``i-1`` and keeps the picked card.  The loop
    always runs (even when it consumes the whole pool), so the RNG call
    sequence matches the client for replay parity.
    """
    pool = list(candidates)
    total = len(pool)
    if total == 0:
        return ()
    count = int(count or 0)
    wanted = total if count <= 0 else min(total, count)
    rng = (battle_state or {}).get("_rules_rng")
    if rng is None or not hasattr(rng, "next"):
        import random
        return tuple(random.sample(pool, wanted))
    picked = []
    for i in range(total, total - wanted, -1):
        index = int(rng.next(i)) % i
        picked.append(pool[index])
        pool[index] = pool[i - 1]
    return tuple(picked)


_LIST_ATTR_BY_KIND = {
    "SourceDrawnTargetTemplate": "DrawnCards",
    "SourceBuriedTargetTemplate": "BuriedCards",
    "AbilityCreatedTargetTemplate": "CreatedCards",
    "VoidedTargetTemplate": "VoidedCards",
}


def _uids(values):
    out = []
    for value in values or ():
        try:
            if value is not None:
                out.append(int(value))
        except (TypeError, ValueError):
            continue
    return tuple(out)


def _list_target_values(battle_state, ability, kind, source_uid, target_spec,
                        db, session_id):
    """Resolve the per-ability stored/drawn/buried/voided/created card lists.

    C# SourceStored/Drawn/Buried/Created/VoidedTargetTemplate enumerate the
    authored ``ListAttrs`` recorded on the ability instance (or the card).
    The port previously left these kinds empty, so "the card you stored /
    voided / created" resolved to no target.
    """
    state = battle_state or {}
    raw_guid = str(getattr(ability, "ability_template_id", "") or "")
    guid = raw_guid.lower()
    from gamedata import DEFAULT_RECORD_STORE
    record = DEFAULT_RECORD_STORE.get(
        "AbilityTargetTemplate", str(target_spec.guid).lower())

    def field(name, default=None):
        return record.field(name, default) if record is not None else default

    if kind == "SourceStoredTargetTemplate":
        values = []
        if bool(field("m_StoredInAbility", True)):
            for key in ("stored_targets", "stored_targets_this_turn"):
                store = state.get(key) or {}
                values.extend(store.get(guid, store.get(raw_guid, ())) or ())
            values.extend((state.get("ability_lists") or {}).get(
                "StoredTargets", ()) or ())
            values.extend(((state.get("list_attrs") or {}).get(guid) or {}).get(
                "StoredTargets", ()) or ())
        if bool(field("m_StoredInCard", True)):
            for key in ("stored_targets_by_card",
                        "stored_targets_by_card_this_turn"):
                store = state.get(key) or {}
                values.extend(store.get(str(int(source_uid or 0)),
                                        store.get(int(source_uid or 0), ())) or ())
            from .statistics import tac_list
            for scope in ("PermanentData", "ThisTurnsData"):
                values.extend(tac_list(
                    state, "cards", int(source_uid or 0), scope,
                    "StoredTargets"))
        return _uids(values)

    list_name = (str(field("m_ListAttrName", "BuriedCards") or
                     "BuriedCards")
                 if kind == "SourceBuriedTargetTemplate" else
                 _LIST_ATTR_BY_KIND.get(kind, ""))
    lists = state.get("list_attrs") or {}
    entries = (lists.get(guid) or {}).get(list_name) or []
    out = list(_uids((state.get("ability_lists") or {}).get(list_name, ())))
    for entry in entries:
        uid = (entry.get("source_uid", entry.get("Id", entry.get("id")))
               if isinstance(entry, dict) else entry)
        try:
            if uid is not None:
                out.append(int(uid))
        except (TypeError, ValueError):
            continue

    if (kind == "VoidedTargetTemplate" and field("m_VoidedBy") is None
            and source_uid is not None):
        # "put each card voided by it into play": the source card's own void
        # ledger, mirroring the legacy resolver's ``voided_by`` fallback.
        out.extend(_uids((state.get("voided_by") or {}).get(
            str(int(source_uid)))))

    if kind == "VoidedTargetTemplate" and field("m_VoidedBy") is not None:
        from .targeting import _source_card
        from .filters import records_filter_matches
        source = _source_card(db, session_id, source_uid,
                              ability.responsible_player_id)
        voided_by_filter = field("m_VoidedBy")
        card_filter = field("m_CardFilter")
        out = []
        voided_by_kind = str(
            getattr(voided_by_filter, "type_name", "")
            or (voided_by_filter.get("_t", "")
                if isinstance(voided_by_filter, dict) else "")
        ).rsplit(".", 1)[-1]
        for voider_uid, targets in (state.get("voided_by") or {}).items():
            try:
                voider = _source_card(
                    db, session_id, int(voider_uid),
                    ability.responsible_player_id)
                # ``IsAbilitySource`` is an identity predicate: the voider must
                # be the ability's own source card.  Evaluating it through the
                # generic filter context picked up a stale event source and
                # matched every voider.
                if voided_by_kind == "IsAbilitySource":
                    matched = int(voider_uid) == int(source_uid or 0)
                else:
                    matched = bool(voider and records_filter_matches(
                        voider, voided_by_filter, source=source,
                        context=state))
                if matched:
                    for uid in _uids(targets):
                        target = _source_card(
                            db, session_id, uid,
                            ability.responsible_player_id)
                        if (not target or str(target.get("location") or "")
                                .lower() != "void"):
                            continue
                        if card_filter and not records_filter_matches(
                                target, card_filter, source=source,
                                context=state,
                                player=int(ability.responsible_player_id or 0)):
                            continue
                        out.append(uid)
            except (TypeError, ValueError):
                continue

    # These special list templates still inherit CardFilter and collection
    # semantics.  The VoidedBy form instead filters the card which caused the
    # void and the client does not apply CollectionFlags in that branch.
    if kind != "VoidedTargetTemplate" or field("m_VoidedBy") is None:
        from .targeting import _source_card, template_zones
        from .filters import records_filter_matches
        zones = {zone.lower() for zone in template_zones({
            "collection_flags": str(field("m_CollectionFlags", "None") or "")})}
        source = _source_card(db, session_id, source_uid,
                              ability.responsible_player_id)
        card_filter = field("m_CardFilter")
        filtered = []
        for uid in dict.fromkeys(out):
            card = _source_card(db, session_id, uid,
                                ability.responsible_player_id)
            if not card:
                continue
            if zones and str(card.get("location") or "").lower() not in zones:
                continue
            if card_filter and not records_filter_matches(
                    card, card_filter, source=source, context=state,
                    player=int(ability.responsible_player_id or 0)):
                continue
            filtered.append(int(uid))
        out = filtered
    return tuple(dict.fromkeys(out))


def _shares_subtype(left, right):
    left = {part.strip().lower() for part in str(left or "").split()
            if part.strip() and part.strip().lower() != "of"}
    right = {part.strip().lower() for part in str(right or "").split()
             if part.strip() and part.strip().lower() != "of"}
    return bool(left & right)


def _current_match_card(db, session_id, uid, owner_id, battle_state):
    """Project the properties used by C# Card.SharesSubtype/Cost."""
    from .targeting import _source_card
    card = _source_card(db, session_id, int(uid), owner_id)
    if not card:
        return None
    try:
        from .static_rules import effective_card_properties, effective_cost
        thresholds, subtype = effective_card_properties(
            db, session_id, battle_state or {}, int(uid))
        card["thresholds"] = thresholds
        card["shards"] = thresholds
        card["subtype"] = subtype
        card["cost"] = effective_cost(
            db, session_id, battle_state or {}, int(uid))
    except (AttributeError, TypeError, ValueError, RuntimeError):
        # Basic card rows are still usable in compact test/older schemas; the
        # native live path supplies the current effective values above.
        pass
    return card


def _match_secondary_values(db, session_id, ability, effect, target_spec,
                            battle_state, champions=None,
                            resolved_by_instance=None):
    """Port of ``MatchSecondaryTargetTemplate.EnumerateLegalTargets``.

    Emits the legal cards that are related to the contingent effect's resolved
    target under the authored SameCost/SameOwner/SharesRace/DoesntShareRace/
    SameName/CantBePreviousTarget flags.  The port previously resolved this
    template as "every card in the collection".
    """
    from gamedata import DEFAULT_RECORD_STORE
    rec = DEFAULT_RECORD_STORE.get("AbilityTargetTemplate", target_spec.guid)
    if rec is None:
        return ()

    def flag(name):
        try:
            return bool(rec.field(name))
        except (AttributeError, TypeError, ValueError):
            return False

    same_cost = flag("m_SameCost")
    same_owner = flag("m_SameOwner")
    shares_race = flag("m_SharesRace")
    doesnt_share_race = flag("m_DoesntShareRace")
    same_name = flag("m_SameName")
    cant_be_prev = flag("m_CantBePreviousTarget")
    sec_value = (effect.get("secondary_target_index", -1)
                 if isinstance(effect, dict)
                 else getattr(effect, "secondary_target_index", -1))
    sec = -1 if sec_value is None else int(sec_value)
    if sec < 0:
        return ()
    raw = _resolved_effect_target_values(
        ability, sec, battle_state, resolved_by_instance)
    from .targeting import legal_targets, _source_card
    secondary = []
    for value in raw:
        if value is None:
            continue
        view = _current_match_card(
            db, session_id, int(value), ability.responsible_player_id,
            battle_state)
        if view:
            secondary.append((int(value), view))
    if not secondary:
        return ()
    candidates = legal_targets(
        db, session_id, ability.responsible_player_id, target_spec.guid,
        ability.source_uid, both_players=True, champions=champions,
        battle_state=battle_state)
    out = []
    for uid in candidates:
        view = _current_match_card(
            db, session_id, int(uid), ability.responsible_player_id,
            battle_state)
        if not view:
            continue
        for other_uid, other in secondary:
            if cant_be_prev and int(uid) == other_uid:
                continue
            if same_cost and int(view.get("cost") or 0) != int(
                    other.get("cost") or 0):
                continue
            if same_owner and int(view.get("user_id") or 0) != int(
                    other.get("user_id") or 0):
                continue
            if same_name and str(view.get("name") or "").lower() != str(
                    other.get("name") or "").lower():
                continue
            if shares_race and not _shares_subtype(
                    view.get("subtype"), other.get("subtype")):
                continue
            if doesnt_share_race and _shares_subtype(
                    view.get("subtype"), other.get("subtype")):
                continue
            out.append(int(uid))
            break
    return tuple(out)


def _resolved_target_values(ability, index, battle_state=None):
    """Return the cards already resolved for one activation mapping index.

    ``ActivateAbility``/``MoveCardToZone`` effects can reference a previous
    effect's target ("the remaining cards", ``m_SecondaryTargetIndex``).  The
    client resolves those from the *target instance* of that mapping, so the
    resolver must read the activation map instead of re-enumerating that
    template's whole legal pool.
    """
    if index is None:
        return ()
    try:
        key = int(index)
    except (TypeError, ValueError):
        return ()
    if key < 0:
        return ()
    maps = (
        getattr(getattr(ability, "activation", None), "target_map", {}) or {},
        (battle_state or {}).get("ability_target_map") or {},
    )
    for mapping in maps:
        value = mapping.get(key, mapping.get(str(key)))
        if value is None:
            continue
        if isinstance(value, (tuple, list, set)):
            return tuple(int(item) for item in value if item is not None)
        return (int(value),)
    return ()


def _referenced_effect_target_index(ability, instance_id):
    """Return the activation target slot for an effect instance reference."""
    try:
        wanted = int(instance_id)
    except (TypeError, ValueError):
        return None
    if wanted < 0:
        return None
    for candidate in getattr(ability, "ordered_effects", ()) or ():
        if isinstance(candidate, dict):
            candidate_id = candidate.get(
                "effect_instance_id", candidate.get("instance_id", -1))
            target_index = candidate.get("target_index", -1)
        else:
            candidate_id = getattr(
                candidate, "effect_instance_id",
                getattr(candidate, "instance_id", -1))
            target_index = getattr(candidate, "target_index", -1)
        try:
            if int(candidate_id) != wanted:
                continue
            target_index = int(target_index)
        except (TypeError, ValueError):
            continue
        return target_index if target_index >= 0 else None
    return None


def _resolved_effect_target_values(ability, instance_id, battle_state=None,
                                  resolved_by_instance=None):
    """Resolve ``m_SecondaryTargetIndex`` through an effect instance.

    The authored field is an effect-instance id, not an activation target-map
    slot. Most abilities happen to use the same numbers for both, which hid
    this distinction until an effect such as Herofall stored its first target
    in instance 1 while using target slot 0.
    """
    try:
        wanted = int(instance_id)
    except (TypeError, ValueError):
        return ()
    if wanted < 0:
        return ()
    if resolved_by_instance is not None:
        value = resolved_by_instance.get(wanted)
        if value is None:
            value = resolved_by_instance.get(str(wanted))
        if value is not None:
            return _uids(value)
    target_index = _referenced_effect_target_index(ability, wanted)
    if target_index is None:
        return ()
    return _resolved_target_values(ability, target_index, battle_state)


def _choose_ai_explicit_target_map(handler, session, battle_state,
                                   player_uid, ai_uid, ability):
    """Ask the native AI evaluator for a fresh explicit target map.

    C# rebuilds an activation for every triggered ability.  The Python
    resolver can enter here with an empty map for a Runic/nested child, so a
    first-legal-target fallback would incorrectly reuse the old target.  A
    ``None`` result means the evaluator could not classify the ability; the
    caller may then retain its compatibility fallback.  An empty mapping is
    authoritative and means there is no legal selected target.
    """
    if int(getattr(ability, "responsible_player_id", 0) or 0) != 0:
        return None
    try:
        from ai_eval import build_evaluator
        evaluator = build_evaluator(
            handler, session, battle_state, ai_uid, player_uid,
            ai_owner_id=0)
        chooser = getattr(evaluator, "choose_ability_target_map", None)
        if not callable(chooser):
            return None
        selected = chooser(
            getattr(ability, "source_uid", None),
            getattr(ability, "ability_template_id", ""))
        if selected is None or not isinstance(selected, dict):
            return None
        normalised = {}
        for index, values in selected.items():
            try:
                index = int(index)
            except (TypeError, ValueError):
                continue
            if not isinstance(values, (tuple, list, set)):
                values = (values,)
            targets = []
            for value in values:
                try:
                    if value is not None:
                        targets.append(int(value))
                except (TypeError, ValueError):
                    continue
            if targets:
                normalised[index] = tuple(targets)
        return normalised
    except Exception:
        # Target selection must not make an otherwise resolvable ability fail
        # because a legacy/incomplete AI snapshot is unavailable.
        return None
def _exhausted_cost_cards(ability):
    """Cards exhausted to pay ``ability``'s additional costs.

    C# records them in the ability instance's ``ExhaustedCards`` list, which
    CountListAttr variables read ("for each troop exhausted this way" on the
    Construction Plans).  The activation keeps each cost's selection by its
    index in the graph's additional cost targets.
    """
    metadata = getattr(ability, "metadata", None)
    graph = getattr(metadata, "graph", None)
    activation = getattr(ability, "activation", None)
    costs = tuple(getattr(graph, "additional_cost_targets", ()) or ())
    if not costs or activation is None:
        return []
    cost_map = getattr(activation, "cost_target_map", {}) or {}
    target_map = getattr(activation, "target_map", {}) or {}
    out = []
    for index, (kind, _guid) in enumerate(costs):
        if str(kind).lower() != "exhaust":
            continue
        selected = cost_map.get(index, cost_map.get(str(index)))
        if not selected:
            selected = target_map.get(index, target_map.get(str(index)))
        if selected is None:
            continue
        if not isinstance(selected, (list, tuple, set)):
            selected = (selected,)
        for value in selected:
            try:
                uid = int(getattr(value, "uid64", value))
            except (TypeError, ValueError):
                continue
            if uid not in out:
                out.append(uid)
    return out


class NativeEffectBackend:
    """Walk one typed ability without entering the legacy BOM resolver."""

    def __call__(self, *, handler, game, session, db, player_uid, ai_uid,
                 battle_state, ability, resume_from_order=None,
                 native_effect=None, effect_groups=None, event_tac=None):
        if native_effect is None:
            from .effects import dispatch
            native_effect = dispatch
        from .effects import SELF_TARGETED_EFFECTS
        from rules_port.context import EffectContext
        from rules_port.conditions import ConditionContext, evaluate_effect_condition
        # Opt-in resolver trace: per-leaf card/event deltas for audits.
        from .trace import begin_effect, end_effect

        old = {key: battle_state.get(key) for key in (
            "resolving_ability", "resolving_source_uid",
            "resolving_owner_id", "resolving_target_uid",
            "resolving_responsible_player_id", "resolving_effect_order",
            "resolving_effect_guid", "resolving_secondary_target_uid",
            "ability_target_map", "_rules_port_native_effect",
            "resolving_ability_instance_id", "_ability_damage_dealt",
            "ability_variables", "session_id", "player_mod_target",
            "player_spell_target", "grant_target", "_skip_transform",
            "rules_port_resolution_depth")}
        # An ability's list attrs (``VoidedCards`` and friends) belong to one
        # resolution: a nested child must see its parent's list, and a
        # finished resolution must not leave its list behind for the next
        # activation to sum a second time.  Mirrors the legacy resolver's
        # save/restore of ``ability_lists``.
        previous_lists = battle_state.get("ability_lists")
        if isinstance(previous_lists, dict):
            # Copy the flat list values too: restoring the dict alone would
            # keep the lists this resolution appended to.
            previous_lists = {
                key: (list(value) if isinstance(value, list) else value)
                for key, value in previous_lists.items()}
        # The legacy resolver guards pathological ability cycles with a depth
        # cap; mirror it so a malformed child chain cannot recurse forever.
        depth = int(battle_state.get("rules_port_resolution_depth", 0) or 0)
        if depth > 16:
            return "resolution depth exceeded"
        # Effect instance ids are only unique within one ability, so a
        # top-level activation starts a fresh m_WasApplied map.  A continuation
        # resumes the same instance and keeps its flags, while a nested child
        # runs against its own map that is restored to the parent's on exit.
        # This mirrors the legacy resolver's per-call local ``applied`` dict;
        # sharing one flat map let an earlier ability's flags satisfy a later
        # ability's contingencies.
        previous_applied = None
        if depth > 0:
            previous_applied = battle_state.get("applied_effects")
            battle_state["applied_effects"] = {}
        elif resume_from_order is None:
            battle_state["applied_effects"] = {}
        battle_state["rules_port_resolution_depth"] = depth + 1
        # Card-count/sum variables evaluate against the live game_cards rows,
        # and ``rules_port.fields.effect_field`` reads the session id from the
        # battle state to query them.  Without it a "for each ..." amount
        # resolved against session 0 and silently became zero (Woeful Webbing
        # summoned no Spider).  The legacy resolver sets the same key; mirror
        # it here, then restore the caller's value on exit.
        battle_state["session_id"] = int(session.session_id)
        exhausted = _exhausted_cost_cards(ability)
        if exhausted:
            battle_state.setdefault("ability_lists", {})[
                "ExhaustedCards"] = exhausted
        battle_state["resolving_ability"] = ability.ability_template_id
        battle_state["resolving_source_uid"] = ability.source_uid
        battle_state["resolving_owner_id"] = int(
            ability.responsible_player_id or 0)
        battle_state["resolving_responsible_player_id"] = int(
            ability.responsible_player_id or 0)
        battle_state["ability_target_map"] = dict(
            getattr(ability.activation, "target_map", {}) or {})
        # EffectInputVariable.GetValue resolves through the active C# ability
        # instance: explicit activation values win, with authored variable
        # defaults as fallback. Target filters use this same scope while the
        # resolver computes automatic targets, so expose it for the duration
        # of this ability and restore a nested parent's scope on exit.
        from .fields import ability_variables
        active_variables = ability_variables(ability)
        active_variables.update(
            getattr(ability.activation, "variables", {}) or {})
        battle_state["ability_variables"] = active_variables
        runtime_instance_id = int(getattr(ability, "instance_id", 0) or 0)
        instance_key = str(runtime_instance_id)
        runtime_values = battle_state.setdefault("ability_runtime_state", {})
        instance_values = runtime_values.setdefault(instance_key, {})
        battle_state["resolving_ability_instance_id"] = runtime_instance_id
        battle_state["_ability_damage_dealt"] = int(
            instance_values.get("DamageDealt", 0) or 0)
        from .targeting import legal_targets
        battle_state["_rules_port_native_effect"] = True
        applied = battle_state.setdefault("applied_effects", {})
        champion_targets = ()
        try:
            champion_targets = tuple(
                getattr(handler, "_champion_targets", lambda: [])() or ())
        except Exception:
            champion_targets = ()
        ai_target_map = None
        ai_target_map_attempted = False

        def field(item, name, default=None):
            value = getattr(item, name, None)
            if value is not None:
                return value
            if isinstance(item, dict):
                aliases = {
                    "guid": "effect_guid",
                    "concrete_type": "effect_type",
                    "condition_guid": "condition_id",
                }
                return item.get(name, item.get(aliases.get(name, ""), default))
            return default

        def condition_passes(effect_value, target_value):
            """Evaluate one effect-instance condition the way C# does."""
            condition_id = str(field(effect_value, "condition_guid", ""))
            if not condition_id or condition_id == "0" * 36:
                return True
            condition_context = ConditionContext(
                db, session, battle_state,
                event_type="AbilityEffectEvent",
                ability_source_uid=ability.source_uid,
                ability_source_owner_id=ability.responsible_player_id,
                trigger_uid=target_value,
                pl_t=player_uid, ai_t=ai_uid,
                champions=champion_targets,
                event_int_attribute=None,
                event_tac=event_tac)
            return bool(evaluate_effect_condition(
                db, condition_id, condition_context))

        def dispatch_nested_effect(parent_context, template, loop_count):
            """Run a typed child as C# Apply(effectInstance) does."""
            from gamedata.semantics import EffectSpec, _effect_param
            from rules_port.fields import _as_dict
            raw = _as_dict(template)
            short_type = str(raw.get("_t", "")).rsplit(".", 1)[-1]
            if not short_type:
                short_type = str(getattr(template, "short_type", "") or "")
            if not short_type:
                return "repeat: invalid nested effect"
            nested_spec = EffectSpec(
                guid=(str(getattr(template, "guid", "") or "") or
                      str(parent_context.effect_guid)),
                concrete_type=short_type,
                operation=short_type.removesuffix("AbilityEffectTemplate")
                or short_type,
                name=str(raw.get("m_Name", "") or ""),
                target_index=-1,
                effect_instance_id=int(battle_state.get(
                    "resolving_effect_order", 0) or 0),
                effect_group_id=0,
                duration=parent_context.effect_duration,
                condition_guid="", optional=False,
                recalculate_targets="UseDefault",
                secondary_target_index=-1,
                output_variables={}, template=template)
            param = _effect_param(nested_spec)
            bases = set()
            for entry in raw.get("_v", ()) or ():
                if isinstance(entry, dict):
                    bases.update(str(name).rsplit(".", 1)[-1]
                                 for name in entry)
            # CardAbilityEffectTemplate.Apply iterates each member of the
            # current target instance. A child which overrides the whole
            # instance (such as another repeat) runs once with that list.
            card_scoped = ("CardAbilityEffectTemplate" in bases and
                           short_type not in SELF_TARGETED_EFFECTS)
            targets = tuple(parent_context.effect_targets or (None,))
            apply_targets = targets if card_scoped else (None,)
            state_keys = ("resolving_target_uid", "player_mod_target",
                          "player_spell_target", "grant_target")
            previous = {key: battle_state.get(key) for key in state_keys}
            results = []
            try:
                for _iteration in range(max(0, int(loop_count or 0))):
                    for target_value in apply_targets:
                        if target_value is None:
                            for key in state_keys:
                                battle_state.pop(key, None)
                        else:
                            target_value = int(target_value)
                            battle_state["resolving_target_uid"] = target_value
                            battle_state["player_mod_target"] = target_value
                            battle_state["player_spell_target"] = target_value
                            battle_state["grant_target"] = target_value
                        child_context = parent_context.nested_effect_context(
                            template, param, targets)
                        result = native_effect(
                            short_type, child_context, {"param": param})
                        if result is None:
                            raise RuntimeError(
                                "RulesPort nested effect has no handler: "
                                f"{short_type}")
                        results.append(str(result))
                        if battle_state.get("resolution_paused"):
                            break
                    if battle_state.get("resolution_paused"):
                        break
            finally:
                for key, value in previous.items():
                    if value is None:
                        battle_state.pop(key, None)
                    else:
                        battle_state[key] = value
            return (f"repeat {short_type} x{max(0, int(loop_count or 0))}: "
                    + "; ".join(results))

        try:
            effects = ability.ordered_effects
            start = int(resume_from_order or 0)
            allowed_groups = (None if effect_groups is None else
                              {int(group) for group in effect_groups})
            # Effect-instance positions let a contingency reject a forward
            # reference the way the client does, and let a continuation mark
            # every already-executed effect as applied (m_WasApplied) so its
            # dependents still run after the pause.
            instance_positions = {
                int(field(item, "effect_instance_id", index)): index
                for index, item in enumerate(effects)}
            # The first card each effect instance resolved, for the
            # ``resolving_secondary_target_uid`` alias a leaf such as a
            # damage-shield reads.
            resolved_by_instance = {}
            for position, effect in enumerate(effects):
                instance_id = int(field(effect, "effect_instance_id", position))
                if position < start:
                    applied[instance_id] = True
                    continue
                battle_state["resolving_effect_order"] = position
                effect_guid = str(field(effect, "guid", "")).lower()
                effect_type = str(field(effect, "concrete_type", ""))
                effect_group = int(field(effect, "effect_group_id", 0))
                if allowed_groups is not None and effect_group not in allowed_groups:
                    continue
                target_index = int(field(effect, "target_index", -1))
                contingent = int(field(
                    effect, "contingent_effect_instance_id", -1))
                if contingent >= 0 and (
                        instance_positions.get(contingent, len(effects))
                        >= position or
                        not applied.get(contingent, False)):
                    applied[instance_id] = False
                    continue
                battle_state["resolving_effect_guid"] = effect_guid
                target_values = ability.activation.target_map.get(
                    target_index, ()) if target_index >= 0 else ()
                native_waiting = False
                if not target_values and target_index >= 0:
                    target_spec = (ability.metadata.targets[target_index]
                                   if target_index < len(ability.metadata.targets)
                                   else None)
                    if target_spec is not None:
                        kind = str(target_spec.target_kind or "")
                        if kind == "AbilitySourceCardTargetTemplate":
                            target_values = (ability.source_uid,)
                        elif kind.endswith("PlayerTargetTemplate"):
                            # A PlayerTargetTemplate identifies a champion in
                            # the client session, not the controller's raw
                            # player id.  In PvE the AI controller is ``0``;
                            # preserving that value made a "You get ..."
                            # GrantAbility look for card UID 0 instead of the
                            # AI champion's synthetic SessionCardId.  Resolve
                            # the slot actually being processed: deriving the
                            # champion from the ability's first template made
                            # Booby Trap's target-index-1 "You" damage resolve
                            # against its target-index-0 "Self" instead and
                            # report "damage: no target".
                            from .targeting import implicit_champion_target
                            player_filter = str(
                                getattr(target_spec, "player_filter", "")
                                or "").lower()
                            champion = implicit_champion_target(
                                db, session, handler, battle_state,
                                opposing=player_filter in {
                                    "opponent", "opposing",
                                    "singleopponent", "multipleopponents"},
                                template_id=getattr(target_spec, "guid", None))
                            target_values = ((champion,) if champion is not None
                                             else ())
                        elif (int(ability.responsible_player_id or 0) == 0 and
                              kind.endswith("AbilityTargetTemplate")
                              and not target_spec.is_auto):
                            if not ai_target_map_attempted:
                                ai_target_map = _choose_ai_explicit_target_map(
                                    handler, session, battle_state, player_uid,
                                    ai_uid, ability)
                                ai_target_map_attempted = True
                            if ai_target_map is not None:
                                target_values = tuple(
                                    ai_target_map.get(target_index, ()))
                                if effect_type == (
                                        "SacrificeCardAbilityEffectTemplate"):
                                    source_uid = ability.source_uid
                                    target_values = tuple(
                                        uid for uid in target_values
                                        if (source_uid is None or
                                            int(uid) != int(source_uid)))
                                if target_values:
                                    # Keep every authored slot chosen by the
                                    # evaluator available to later effects in
                                    # this activation, including shared target
                                    # instances that were initially empty.
                                    ability.activation.target_map.update(
                                        ai_target_map)
                                    battle_state["ability_target_map"].update(
                                        ai_target_map)
                            else:
                                # If the evaluator cannot build a complete
                                # snapshot, preserve the old native fallback.
                                # Authored random targets still use the session
                                # RNG and explicit non-random targets take the
                                # first legal candidate.
                                both_players = str(
                                    target_spec.player_filter or "").lower() not in {
                                        "self", "you", "controller"}
                                candidates = tuple(legal_targets(
                                    db, session.session_id,
                                    ability.responsible_player_id,
                                    target_spec.guid, ability.source_uid,
                                    both_players=both_players,
                                    champions=(getattr(
                                        handler, "_champion_targets", lambda: [])()
                                               or []),
                                    battle_state=battle_state))
                                if effect_type == "SacrificeCardAbilityEffectTemplate":
                                    # "another troop you control" never sacrifices
                                    # the source as its own payment.
                                    source_uid = ability.source_uid
                                    candidates = tuple(
                                        uid for uid in candidates
                                        if (source_uid is None or
                                            int(uid) != int(source_uid)))
                                if target_spec.is_random:
                                    candidates = _random_target_sample(
                                        candidates,
                                        int(target_spec.resolved_maximum(
                                            ability.activation.variables) or 0),
                                        battle_state)
                                    target_values = candidates
                                else:
                                    maximum = int(target_spec.resolved_maximum(
                                        ability.activation.variables) or 0)
                                    target_values = (candidates[:maximum]
                                                     if maximum > 0 else candidates[:1])
                            if ai_target_map is not None and not target_values:
                                # A complete evaluator result is authoritative:
                                # no legal selected target disables this effect
                                # instead of letting a leaf fall back to the
                                # resolving source card.
                                applied[instance_id] = condition_passes(
                                    effect, None)
                                for key in ("resolving_target_uid",
                                            "player_mod_target",
                                            "player_spell_target",
                                            "grant_target"):
                                    battle_state.pop(key, None)
                                continue
                        elif kind == "AbilityTriggerCardTargetTemplate":
                            from .targeting import _target_field
                            selector = str(_target_field(
                                target_spec.guid, "m_TriggerSelector",
                                "TriggerSource") or "TriggerSource").rsplit(
                                    ".", 1)[-1]
                            key = ("resolving_trigger_target_uid"
                                   if selector == "TriggerTarget" else
                                   "resolving_trigger_source_uid"
                                   if selector == "TriggerSource" else None)
                            selected = battle_state.get(key) if key else None
                            if selected is None:
                                # Some event classes carry only one of the
                                # trigger cards; the legacy resolver's
                                # transient target is the next fallback, then
                                # the ability source (the card that raised the
                                # event for #TRIGGER_SOURCE#).
                                selected = (
                                    battle_state.get("player_spell_target")
                                    or battle_state.get("player_mod_target")
                                    or battle_state.get("resolving_target_uid"))
                            if selected is None:
                                selected = ability.source_uid
                            target_values = ((int(selected),)
                                             if selected is not None else ())
                        elif kind in ("SourceDrawnTargetTemplate",
                                      "SourceBuriedTargetTemplate",
                                      "SourceStoredTargetTemplate",
                                      "AbilityCreatedTargetTemplate",
                                      "VoidedTargetTemplate"):
                            target_values = _list_target_values(
                                battle_state, ability, kind,
                                ability.source_uid, target_spec, db,
                                session.session_id)
                            if not target_values:
                                # C# disables an effect whose authored list
                                # target enumerates nothing; it never falls
                                # back to the source card.  The instance still
                                # counts as applied when its condition holds,
                                # matching the client's m_WasApplied
                                # bookkeeping for contingent effects.
                                applied[instance_id] = condition_passes(
                                    effect, None)
                                for key in ("resolving_target_uid",
                                            "player_mod_target",
                                            "player_spell_target",
                                            "grant_target"):
                                    battle_state.pop(key, None)
                                continue
                        elif kind == "SecondaryTargetTemplate":
                            # C# SecondaryTargetTemplate: the input cards are
                            # the resolved outputs of the contingent effect's
                            # target (m_SecondaryTargetIndex), filtered by the
                            # template card filter.
                            sec = int(field(effect, "secondary_target_index", -1))
                            raw = (_resolved_effect_target_values(
                                ability, sec, battle_state,
                                resolved_by_instance) if sec >= 0 else ())
                            from .targeting import filter_resolved_targets
                            target_values = filter_resolved_targets(
                                db, session.session_id,
                                ability.responsible_player_id,
                                target_spec.guid, ability.source_uid, raw,
                                battle_state)
                        elif kind == "MatchSecondaryTargetTemplate":
                            target_values = _match_secondary_values(
                                db, session.session_id, ability, effect,
                                target_spec, battle_state,
                                champions=(getattr(
                                    handler, "_champion_targets",
                                    lambda: [])() or []),
                                resolved_by_instance=resolved_by_instance)
                        elif target_spec.target_kind == "SourceRevealedTargetTemplate":
                            from .targeting import (revealed_target_uids,
                                                    _target_ignore_acted_on)
                            acted_on = ()
                            if _target_ignore_acted_on(target_spec.guid):
                                sec_value = field(
                                    effect, "secondary_target_index", -1)
                                sec_index = (-1 if sec_value is None
                                             else int(sec_value))
                                # "the remaining cards" ignores what the
                                # referenced mapping already acted on: the
                                # card the player just chose, not that
                                # template's whole legal candidate pool.
                                acted_on = _resolved_effect_target_values(
                                    ability, sec_index, battle_state,
                                    resolved_by_instance)
                                referenced_target_index = (
                                    _referenced_effect_target_index(
                                        ability, sec_index))
                                if (referenced_target_index is not None and
                                        referenced_target_index < len(
                                            ability.metadata.targets)):
                                    sec_spec = ability.metadata.targets[
                                        referenced_target_index]
                                    acted_on = acted_on or tuple(revealed_target_uids(
                                        db, session.session_id,
                                        ability.responsible_player_id,
                                        ability.source_uid, sec_spec.guid,
                                        battle_state.get("revealed_cards") or [],
                                        battle_state=battle_state))
                            candidates = revealed_target_uids(
                                db, session.session_id,
                                ability.responsible_player_id, ability.source_uid,
                                target_spec.guid,
                                battle_state.get("revealed_cards") or [],
                                battle_state=battle_state, acted_on_uids=acted_on)
                            if not candidates:
                                # Nothing revealed matches (no artifact among
                                # the cards, or every card was already taken).
                                # C# enumerates an empty target list and the
                                # effect does nothing; it must not fall back
                                # to the source card (Gearsmith moved itself
                                # into the deck).
                                applied[instance_id] = condition_passes(
                                    effect, None)
                                for key in ("resolving_target_uid",
                                            "player_mod_target",
                                            "player_spell_target",
                                            "grant_target"):
                                    battle_state.pop(key, None)
                                continue
                            # A SourceRevealed target is input-bearing only when
                            # the authored template asks the player to choose.
                            # Oakhenge's child ability targets "a revealed troop"
                            # (a real picker) and then "the remaining cards"
                            # (m_IsAutoTarget): prompting for the second one asked
                            # the player to pick twice and re-moved the chosen
                            # card, and the extra pause left the spell on the
                            # chain without priority.
                            if (candidates and target_spec.requires_input
                                    and int(ability.responsible_player_id or 0) != 0):
                                prompt = getattr(handler, "_prompt_revealed_choice", None)
                                if callable(prompt):
                                    continuation = {
                                        "ability_instance_id": int(ability.instance_id),
                                        "ability_guid": ability.ability_template_id,
                                        "source_uid": int(ability.source_uid or 0),
                                        "owner_id": int(ability.responsible_player_id or 0),
                                        "target_map": {
                                            str(key): value for key, value in
                                            ability.activation.target_map.items()},
                                        "variables": dict(ability.activation.variables or {}),
                                        "resume_effect_order": int(position),
                                        "target_index": int(target_index),
                                    }
                                    if battle_state.get("_choice_parent"):
                                        continuation["parent"] = dict(
                                            battle_state["_choice_parent"])
                                    prompt(
                                        game, session, player_uid, ai_uid,
                                        battle_state, ability.ability_template_id,
                                        int(ability.source_uid or 0),
                                        int(ability.responsible_player_id),
                                        candidates,
                                        list(battle_state.get("revealed_cards") or []),
                                        # "Up to one" (minimum 0) may be
                                        # declined like an optional target.
                                        optional=bool(
                                            target_spec.optional or
                                            int(target_spec.minimum or 0) == 0),
                                        continuation=continuation)
                                    battle_state["resolution_paused"] = True
                                    native_waiting = True
                            elif not candidates:
                                # C# disables an effect whose authored
                                # SourceRevealed target enumerates nothing.  The
                                # resolver previously rewrote the empty list to
                                # ``(None,)`` and the MoveCardToZone leaf's
                                # source fallback then moved the resolving card
                                # itself: Oakhenge's "put a revealed troop into
                                # your hand" put the spell into hand when the
                                # top five held no troop, and the spell was
                                # never discarded from CastSpells.
                                applied[instance_id] = condition_passes(
                                    effect, None)
                                for key in ("resolving_target_uid",
                                            "player_mod_target",
                                            "player_spell_target",
                                            "grant_target"):
                                    battle_state.pop(key, None)
                                continue
                            else:
                                # C# SourceRevealedTargetTemplate leaves
                                # m_MaximumTargetCount unset (int.MaxValue),
                                # so a 0/absent maximum means "all of them",
                                # not a single card.
                                _max = int(target_spec.maximum or 0)
                                target_values = tuple(
                                    candidates[:_max] if _max > 0 else candidates)
                        elif target_spec.is_auto:
                            from .targeting import target_uses_both_players
                            both_players = target_uses_both_players(
                                db, target_spec.guid)
                            candidates = tuple(legal_targets(
                                db, session.session_id,
                                int(ability.responsible_player_id or 0),
                                target_spec.guid, ability.source_uid,
                                both_players=both_players,
                                champions=(getattr(handler, "_champion_targets",
                                                   lambda: [])() or []),
                                battle_state=battle_state))
                            if not candidates:
                                # C# GetAutoTargets yields an empty set when no
                                # card matches the filter.  Falling through to
                                # ``target_values = (None,)`` made target-aware
                                # effects fall back to ``resolving_source_uid``;
                                # Corinth's Shifted Paradigm (auto-targets "your
                                # hand"/"your crypt") then moved the champion
                                # into the deck.  An effect with no auto-target
                                # card simply does nothing.
                                continue
                            if target_spec.is_random:
                                # A random auto-target (e.g. Infernal Professor's
                                # "a random non-resource card from your deck")
                                # must resolve to a bounded random sample, not
                                # every legal card.  Iterating the whole pool
                                # moved the entire deck into hand.
                                candidates = _random_target_sample(
                                    candidates,
                                    int(target_spec.resolved_maximum(
                                        ability.activation.variables) or 0),
                                    battle_state)
                            else:
                                _max = int(target_spec.resolved_maximum(
                                    ability.activation.variables) or 0)
                                if _max > 0 and len(candidates) > _max:
                                    # C# GetAutoTargets truncates a non-random
                                    # auto-target to GetMaximumTargetCount.
                                    candidates = candidates[:_max]
                            target_values = candidates
                        if target_values:
                            # Effects that share a target-template index share
                            # one client AbilityTargetInstance.  RecalculateTargets
                            # refreshes that instance before its first effect;
                            # it does not choose a different random card for
                            # every subsequent effect using the same mapping.
                            # Keep the resolved automatic target on this
                            # activation so a move followed by modifiers (for
                            # example Infernal Professor) remains one atomic
                            # card operation.
                            resolved_targets = tuple(target_values)
                            ability.activation.target_map[target_index] = \
                                resolved_targets
                            battle_state["ability_target_map"][target_index] = \
                                resolved_targets
                            target_values = resolved_targets
                if native_waiting:
                    break
                if not target_values:
                    target_values = (None,)
                if not isinstance(target_values, (tuple, list)):
                    target_values = (target_values,)
                if (effect_type == "RevealCardsAbilityEffectTemplate" and
                        len(target_values) > 1):
                    # C# RevealCards reveals the whole target set in one
                    # CardsRevealed event, and reveal_cards selects that set
                    # itself.  Running it once per card ("the top three cards
                    # of your deck") revealed the same three cards three
                    # times, replaying the client's reveal presentation.
                    target_values = tuple(target_values[:1])
                effect_targets = tuple(target_values)
                resolved_by_instance[instance_id] = tuple(
                    int(value) for value in effect_targets
                    if value is not None)
                effect_param = field(effect, "param", "")
                # ``m_SecondaryTargetIndex`` names an earlier effect instance,
                # and the client answers it with that instance's resolved
                # card (e.g. the damage dealer a shield is restricted to).
                # Expose the same alias the legacy resolver published; a leaf
                # with no such reference must not inherit a previous effect's.
                secondary_index = int(field(effect, "secondary_target_index", -1))
                secondary_values = (resolved_by_instance.get(secondary_index, ())
                                    if secondary_index >= 0 else ())
                if secondary_values:
                    battle_state["resolving_secondary_target_uid"] = int(
                        secondary_values[0])
                else:
                    battle_state.pop("resolving_secondary_target_uid", None)
                # C# runs a non-card AbilityEffectTemplate once per effect
                # instance with that instance's whole target list, while the
                # card-scoped leaves below expect one call per target.  A
                # self-targeted effect resolves its own set from Records, so
                # looping its resolved targets re-runs the whole operation:
                # Oakhenge's reveal fired once per revealed card.
                if effect_type in SELF_TARGETED_EFFECTS:
                    target_values = (next(
                        (value for value in target_values
                         if value is not None), None),)
                for target in target_values:
                    if target is None:
                        battle_state.pop("resolving_target_uid", None)
                        for key in ("player_mod_target", "player_spell_target",
                                    "grant_target"):
                            battle_state.pop(key, None)
                    else:
                        target = int(target)
                        battle_state["resolving_target_uid"] = target
                        battle_state["player_mod_target"] = target
                        battle_state["player_spell_target"] = target
                        battle_state["grant_target"] = target
                    effect_context = EffectContext.from_rules_port(
                        game, session, db, handler, player_uid, ai_uid,
                        battle_state, effect_guid, effect_param,
                        ability=ability, effect_targets=effect_targets,
                        nested_effect_dispatch=dispatch_nested_effect)
                    if not condition_passes(effect, target):
                        applied[instance_id] = False
                        # Incantation-style BOMs put the five-counter gate on
                        # the remove-counters effect while the following
                        # transform leaf has no condition of its own; carry
                        # the failed gate forward instead of transforming the
                        # first target anyway.
                        if effect_type == "CardModifierAbilityEffectTemplate":
                            try:
                                modifier = json.loads(effect_param or "{}")
                            except (TypeError, ValueError,
                                    json.JSONDecodeError):
                                modifier = {}
                            if (str(modifier.get("property") or "") == "counter"
                                    and int(modifier.get("amount") or 0) <= 0):
                                battle_state["_skip_transform"] = True
                        continue
                    trace_effect = {
                        "effect_guid": effect_guid,
                        "effect_type": effect_type,
                        "effect_order": position,
                    }
                    trace = begin_effect(
                        db, session, game, battle_state, trace_effect, target)
                    try:
                        result = native_effect(
                            effect_type, effect_context, effect)
                    except Exception as exc:
                        end_effect(
                            db, session, game, battle_state, trace, error=exc)
                        raise
                    if result is None:
                        raise RuntimeError(
                            "RulesPort native effect has no handler: "
                            f"{effect_type} ({effect_guid})")
                    end_effect(
                        db, session, game, battle_state, trace, result=result)
                    applied[instance_id] = True
                    battle_state.setdefault("rules_port_effect_results", []).append(
                        {"instance_id": instance_id, "result": str(result)})
                if battle_state.get("resolution_paused"):
                    battle_state["rules_port_resume_effect_order"] = position
                    break
        finally:
            runtime_values = battle_state.get("ability_runtime_state") or {}
            instance_values = runtime_values.get(instance_key)
            if isinstance(instance_values, dict):
                if battle_state.get("resolution_paused"):
                    instance_values["DamageDealt"] = int(
                        battle_state.get("_ability_damage_dealt", 0) or 0)
                else:
                    runtime_values.pop(instance_key, None)
                    if not runtime_values:
                        battle_state.pop("ability_runtime_state", None)
            cache = battle_state.get("ability_variable_cache") or {}
            if battle_state.get("resolution_paused"):
                pass
            else:
                cache.pop(instance_key, None)
                if not cache:
                    battle_state.pop("ability_variable_cache", None)
            for key, value in old.items():
                if value is None:
                    battle_state.pop(key, None)
                else:
                    battle_state[key] = value
            if previous_lists is None:
                battle_state.pop("ability_lists", None)
            else:
                battle_state["ability_lists"] = previous_lists
            if depth > 0:
                # A nested child ran against its own m_WasApplied map; give
                # the parent's back so the remainder of the parent walk sees
                # the flags it set before the child was activated.
                if previous_applied is None:
                    battle_state.pop("applied_effects", None)
                else:
                    battle_state["applied_effects"] = previous_applied


class PortAbilityResolver:
    """Resolve one port-owned ability and persist its UI continuation."""

    def __init__(self, handler, game, game_session, db, player_uid, ai_uid,
                 battle_state: dict, *, native_effect=None,
                 effect_groups=None, event_tac=None) -> None:
        self.handler = handler
        self.game = game
        self.game_session = game_session
        self.db = db
        self.player_uid = player_uid
        self.ai_uid = ai_uid
        self.battle_state = battle_state
        self.effect_groups = effect_groups
        self.event_tac = {}
        for key, value in (event_tac or {}).items():
            try:
                self.event_tac[int(key)] = value
            except (TypeError, ValueError):
                continue
        self.effect_backend = NativeEffectBackend()
        if native_effect is None:
            from .effects import dispatch
            native_effect = dispatch
        self.native_effect = native_effect

    def __call__(self, ability) -> AbilityResolutionState:
        # A reconnect or hot reload may replace the mutable checkpoint after
        # this resolver was constructed. Resolve against the session's current
        # RulesPort state, never the dictionary captured at attach time.
        from .persistence import load_state
        requested_resume = self.battle_state.get(
            "rules_port_resume_effect_order")
        current_state = load_state(self.game_session)
        if isinstance(current_state, dict) and current_state:
            self.battle_state = current_state
        # The continuation caller may have supplied a newer in-memory offset
        # than the last checkpoint. Preserve it across the reload. Conversely
        # an explicitly fresh nested child must not inherit its parent's
        # offset and skip its own effect 0.
        if requested_resume is None:
            self.battle_state.pop("rules_port_resume_effect_order", None)
        else:
            self.battle_state["rules_port_resume_effect_order"] = int(
                requested_resume)
        rng = getattr(self.game_session, "random_number_generator", None)
        if rng is not None:
            self.battle_state["_rules_rng"] = rng
        previous_strict = self.battle_state.get("_rules_port_strict_effects")
        self.battle_state["_rules_port_strict_effects"] = True
        resume_order = self.battle_state.get("rules_port_resume_effect_order")
        # A continuation may legitimately have no target map.  Choice-zone
        # effects save only the effect offset because the selected card is
        # supplied through the persisted choice state.  Never restart the
        # parent ability merely because its target map is empty.
        if resume_order is not None:
            self.battle_state.pop("resolution_paused", None)
        try:
            self.effect_backend(
                handler=self.handler, game=self.game, session=self.game_session,
                db=self.db, player_uid=self.player_uid, ai_uid=self.ai_uid,
                battle_state=self.battle_state, ability=ability,
                resume_from_order=resume_order, native_effect=self.native_effect,
                effect_groups=self.effect_groups,
                event_tac=self.event_tac)
        finally:
            if previous_strict is None:
                self.battle_state.pop("_rules_port_strict_effects", None)
            else:
                self.battle_state["_rules_port_strict_effects"] = previous_strict
        port_session = getattr(self.game_session, "_rules_port_session", None)
        if self.battle_state.get("pending_discard_ability") and port_session is not None:
            pending = self.battle_state.get("pending_discard_continuation")
            child_guid = str((pending or {}).get("ability_guid") or "").lower()
            current_guid = str(ability.ability_template_id or "").lower()
            if (self.battle_state.get("resolution_paused") and
                    isinstance(pending, dict) and child_guid and
                    child_guid != current_guid):
                paused_order = self.battle_state.get(
                    "rules_port_resume_effect_order")
                if paused_order is None:
                    paused_order = self.battle_state.get(
                        "resolving_effect_order", 0)
                continuation = ability.continuation(
                    resume_effect_order=int(paused_order or 0) + 1)
                activation = continuation.pop("activation", {})
                continuation["target_map"] = dict(
                    activation.get("target_map") or {})
                continuation["variables"] = dict(
                    activation.get("variables") or
                    continuation.get("variables") or {})
                # ActivateAbility effects may nest. Keep each suspended
                # parent in order so the picker answer resumes the child,
                # then its parent, then any enclosing ability.
                cursor = pending
                ancestors = {(child_guid, int(pending.get(
                    "ability_instance_id", pending.get("instance_id", 1)) or 1))}
                while isinstance(cursor.get("parent"), dict):
                    cursor = cursor["parent"]
                    ancestors.add((str(cursor.get("ability_guid") or "").lower(),
                                   int(cursor.get("ability_instance_id", 1) or 1)))
                identity = (current_guid, int(ability.instance_id))
                if identity not in ancestors:
                    cursor["parent"] = continuation
            port_session.pending_activation = {
                "ability_instance_id": int(ability.instance_id),
                "responsible_player_id": int(ability.responsible_player_id),
                "continuation": ability.continuation(), "prompts": (),
            }
            port_session.persist()
        elif resume_order is not None and not self.battle_state.get(
                "resolution_paused"):
            for key in ("rules_port_resume_effect_order", "pending_discard_ability",
                        "pending_discard_target_template", "pending_discard_scid"):
                self.battle_state.pop(key, None)
        if self.battle_state.get("resolution_paused"):
            return AbilityResolutionState.WAITING_FOR_INPUT
        return AbilityResolutionState.COMPLETED


def build_port_ability(ability_guid, source_uid, owner_id, *, instance_id=1,
                       target_map=None, variables=None, cost_target_map=None):
    """Build one typed ability instance from the authoritative Records graph."""
    from gamedata import DEFAULT_RECORD_STORE, ability_graph
    from gamedata.play_plan import AbilityInstance as MetadataAbility
    from .abilities import AbilityInstance

    graph = ability_graph(DEFAULT_RECORD_STORE, str(ability_guid).lower())
    if graph is None:
        raise RuntimeError(f"ability {ability_guid} is missing from Records")
    metadata = MetadataAbility.from_graph(
        graph, source_uid=None if source_uid is None else int(source_uid),
        owner_id=int(owner_id or 0), responsible_player_id=owner_id)
    ability = AbilityInstance(
        instance_id=int(instance_id), metadata=metadata,
        activating_player_id=owner_id, responsible_player_id=owner_id)
    activation = {"target_map": target_map or {},
                  "variables": variables or {}}
    if cost_target_map:
        # Additional-cost selections (exhaust/sacrifice/...) paid when the
        # ability was activated; effects such as Construction Plans count
        # them ("for each troop exhausted this way").
        activation["cost_target_map"] = cost_target_map
    ability.bind_activation(activation)
    return ability


def resolve_port_ability(handler, game, session, db, player_uid, ai_uid,
                         battle_state, ability_guid, source_uid, owner_id,
                         *, target_map=None, variables=None,
                         resume_from_order=None, instance_id=1,
                         native_effect=None,
                         effect_groups=None, event_tac=None,
                         cost_target_map=None):
    """Resolve a persisted continuation through the port-owned lifecycle."""
    log_targets = target_map
    if not log_targets:
        fallback = ((battle_state or {}).get("resolving_target_uid")
                    or (battle_state or {}).get("player_spell_target")
                    or (battle_state or {}).get("player_mod_target"))
        if fallback is not None:
            log_targets = {0: fallback}
    _log_ability_start(handler, db, ability_guid, source_uid, owner_id,
                       log_targets)
    ability = build_port_ability(
        ability_guid, source_uid, owner_id, instance_id=instance_id,
        target_map=target_map, variables=variables,
        cost_target_map=cost_target_map)
    # The resume offset is persisted in the existing session snapshot because
    # the continuation may have crossed a reconnect boundary.
    if resume_from_order is not None:
        battle_state["rules_port_resume_effect_order"] = int(resume_from_order)
    else:
        # A nested child is a fresh ability. Do not let it inherit the parent
        # continuation offset from the shared battle-state dictionary.
        battle_state.pop("rules_port_resume_effect_order", None)
    if event_tac is None and (battle_state or {}).get(
            "_spell_played_from_hand"):
        from .tac import _tac_attr_hash
        event_tac = {_tac_attr_hash("PlayedFromHand"): 1}
    return PortAbilityResolver(
        handler, game, session, db, player_uid, ai_uid, battle_state,
        native_effect=native_effect,
        effect_groups=effect_groups, event_tac=event_tac)(ability)


def resume_ability_continuation_parents(
        handler, game, session, db, player_uid, ai_uid, battle_state,
        continuation):
    """Resume nested parents after an invoked child ability finishes."""
    state = battle_state or {}
    continuation = dict(continuation or {})
    parent = ((continuation or {}).get("parent")
              if isinstance(continuation, dict) else None)
    if not isinstance(parent, dict):
        # A class-23 picker can outlive the process that created it. Older
        # checkpoints stored the child continuation but not the suspended
        # ActivateAbility parent, leaving the parent chain item to replay on
        # every pass. Rebuild that one edge from the persisted chain
        # descriptor and authored ActivateAbility metadata.
        try:
            from gamedata import DEFAULT_RECORD_STORE, ability_graph
            child_guid = str(continuation.get("ability_guid") or "").lower()
            chain_id = int(state.get("paused_chain_instance_id", 0) or 0)
            item = next((entry for entry in reversed(state.get("stack") or [])
                         if int(entry.get("instance_id", -1)) == chain_id), None)
            parent_guid = str((item or {}).get("ability_guid") or "").lower()
            parent_graph = (ability_graph(DEFAULT_RECORD_STORE, parent_guid)
                            if parent_guid else None)
            if (chain_id > 0 and child_guid and item is not None and
                    parent_graph is not None and
                    parent_guid != child_guid):
                for order, effect in enumerate(parent_graph.effects):
                    if effect.concrete_type not in (
                            "ActivateAbilityEffectTemplate",
                            "ActivatePowerAbilityEffectTemplate"):
                        continue
                    invoked = getattr(effect.template, "m_AbilityToInvoke", None)
                    invoked_guid = (invoked.get("m_Guid")
                                    if isinstance(invoked, dict) else
                                    getattr(invoked, "m_Guid", None))
                    if str(invoked_guid or "").lower() != child_guid:
                        continue
                    activation = (item.get("activation_data") or {})
                    parent = {
                        "ability_instance_id": chain_id,
                        "ability_guid": parent_guid,
                        "source_uid": int(continuation.get(
                            "source_uid", item.get("source_uid", 0)) or 0),
                        "owner_id": int(continuation.get(
                            "owner_id", item.get("owner_id", 0)) or 0),
                        "target_map": dict(
                            activation.get("target_map") or {}),
                        "variables": dict(
                            activation.get("variables") or {}),
                        "resume_effect_order": int(order) + 1,
                    }
                    continuation["parent"] = parent
                    break
        except (TypeError, ValueError, AttributeError):
            parent = None
    result = AbilityResolutionState.COMPLETED
    while isinstance(parent, dict) and parent.get("ability_guid"):
        parent_guid = str(parent.get("ability_guid") or "").lower()
        activation = parent.get("activation") or {}
        target_map = parent.get("target_map")
        variables = parent.get("variables")
        if isinstance(activation, dict):
            if target_map is None:
                target_map = activation.get("target_map")
            if variables is None:
                variables = activation.get("variables")
        result = resolve_port_ability(
            handler, game, session, db, player_uid, ai_uid, state,
            parent_guid, parent.get("source_uid"),
            int(parent.get("owner_id", 0) or 0),
            target_map=target_map or {}, variables=variables or {},
            resume_from_order=int(parent.get("resume_effect_order", 0) or 0),
            instance_id=int(parent.get(
                "ability_instance_id", parent.get("instance_id", 1)) or 1),
            event_tac=parent.get("event_tac"))
        if state.get("resolution_paused"):
            return result
        parent = parent.get("parent")

    if not state.get("resolution_paused"):
        completed = int(state.pop("paused_chain_instance_id", 0) or 0)
        if completed:
            state["completed_chain_instance_id"] = completed
        # The class-23 prompt was projected as a host event rather than as a
        # normal RulesPort activation response. Release the activation slot
        # once the child and every suspended parent have resumed; otherwise
        # the next player transaction can still look like a reply to the old
        # prompt after its chain item has been completed.
        port_session = getattr(session, "_rules_port_session", None)
        pending_activation = getattr(port_session, "pending_activation", None)
        if port_session is not None and isinstance(pending_activation, dict):
            try:
                pending_id = int(pending_activation.get(
                    "ability_instance_id", -1))
                continuation_id = int((continuation or {}).get(
                    "ability_instance_id",
                    (continuation or {}).get("instance_id", -2)))
            except (TypeError, ValueError):
                pending_id = continuation_id = -1
            if pending_id == continuation_id:
                port_session.pending_activation = None
                port_session.persist()
    return result


def resolve_port_played_spell(game, session, db, handler, player_uid, ai_uid,
                               battle_state, ability_guids, *,
                               activations=None, played_from_hand=False):
    """Resolve all authored abilities on a spell through the port lifecycle.

    Card play is a multi-ability activation, not a special legacy resolver.
    Each graph is still sourced from Records, while scheduling, continuations,
    and effect dispatch remain RulesPort-owned.
    """
    from gamedata import ActivationData, DEFAULT_RECORD_STORE, ability_graph
    from .tac import _tac_attr_hash, tac_int

    bstate = battle_state or {}
    spell_event_tac = ({_tac_attr_hash("PlayedFromHand"): 1}
                       if played_from_hand else {})
    previous_played_from_hand = bstate.get("_spell_played_from_hand")
    bstate["_spell_played_from_hand"] = bool(played_from_hand)
    from .persistence import save_state
    save_state(session, bstate)
    target_uid = bstate.get("player_spell_target")
    source_uid = bstate.get("resolving_source_uid")
    owner_id = bstate.get("resolving_owner_id")
    if source_uid is not None:
        # A played card's real owner is authoritative: a stale
        # resolving_owner_id (for example 0 left by an earlier AI trigger)
        # must not resolve the spell as the opponent's.
        from pvp_db import db_card_owner_id
        source_owner = db_card_owner_id(
            session.session_id, int(source_uid), conn=db)
        if source_owner is not None:
            owner_id = source_owner
    if owner_id is None:
        owner_id = handler.user_profile["id"] if handler.user_profile else 0

    activation_map = {
        str(key).lower(): ActivationData.from_dict(value)
        for key, value in (activations or {}).items()
    }
    previous = bstate.get("_esc_counted_this_resolution")
    bstate["_esc_counted_this_resolution"] = False
    previous_lists = bstate.get("ability_lists")
    if isinstance(previous_lists, dict):
        previous_lists = {
            key: (list(value) if isinstance(value, list) else value)
            for key, value in previous_lists.items()}
    logs = []
    try:
        guid_values = list(ability_guids or [])
        for ability_index, guid_value in enumerate(guid_values):
            instance_id = ability_index + 1
            guid = str(guid_value).lower()
            graph = ability_graph(DEFAULT_RECORD_STORE, guid)
            if graph is None:
                raise RuntimeError(f"played ability {guid} is missing from Records")
            if (tac_int(graph.serialized_tac, "Scrounge", 0) and
                    not ((bstate.get("ability_lists") or {}).get(
                        "VoidedCards") or [])):
                continue
            activation = activation_map.get(guid)
            target_map = (dict(activation.target_map)
                          if activation is not None else {})
            if not target_map and target_uid is not None:
                for target_index, target in enumerate(graph.targets):
                    if target.requires_input:
                        target_map[target_index] = (int(target_uid),)
                        break
            state = resolve_port_ability(
                handler, game, session, db, player_uid, ai_uid, bstate,
                guid, source_uid, owner_id, target_map=target_map,
                variables=(activation.variables if activation is not None
                           else None), instance_id=instance_id,
                event_tac=spell_event_tac)
            logs.append(state.name if hasattr(state, "name") else state)
            if bstate.get("resolution_paused"):
                continuation = None
                for key in ("pending_deck_search", "pending_choice",
                            "pending_trigger", "pending_conversation",
                            "pending_discard_continuation"):
                    pending = bstate.get(key)
                    if not isinstance(pending, dict):
                        continue
                    candidate = pending.get("continuation")
                    if isinstance(candidate, dict):
                        continuation = candidate
                        break
                    if key == "pending_discard_continuation":
                        continuation = pending
                        break
                remaining = guid_values[ability_index + 1:]
                if continuation is not None and remaining:
                    # A picker can suspend one printed ability while later
                    # abilities on the played card remain pending. Preserve
                    # the original card-play event context through that
                    # continuation so authored conditions such as
                    # PlayedFromHand still evaluate when the sibling resumes.
                    continuation["event_tac"] = dict(spell_event_tac)
                    tail = continuation
                    visited = set()
                    while (isinstance(tail.get("parent"), dict) and
                           id(tail) not in visited):
                        visited.add(id(tail))
                        tail = tail["parent"]
                    for next_index, next_guid_value in enumerate(
                            remaining, ability_index + 2):
                        next_guid = str(next_guid_value).lower()
                        next_activation = activation_map.get(next_guid)
                        parent = {
                            "ability_guid": next_guid,
                            "source_uid": int(source_uid or 0),
                            "owner_id": int(owner_id or 0),
                            "ability_instance_id": next_index,
                            "instance_id": next_index,
                            "target_map": (dict(next_activation.target_map)
                                           if next_activation is not None else {}),
                            "variables": (dict(next_activation.variables or {})
                                          if next_activation is not None else {}),
                            "resume_effect_order": 0,
                            "event_tac": dict(spell_event_tac),
                        }
                        tail["parent"] = parent
                        tail = parent
                break
    finally:
        if not bstate.get("resolution_paused"):
            if previous_lists is None:
                bstate.pop("ability_lists", None)
            else:
                bstate["ability_lists"] = previous_lists
        if previous is None:
            bstate.pop("_esc_counted_this_resolution", None)
        else:
            bstate["_esc_counted_this_resolution"] = previous
        if not bstate.get("resolution_paused"):
            if previous_played_from_hand is None:
                bstate.pop("_spell_played_from_hand", None)
            else:
                bstate["_spell_played_from_hand"] = previous_played_from_hand
    return "; ".join(str(value) for value in logs if value)


def resolve_port_trigger(handler, game, session, db, player_uid, ai_uid,
                         battle_state, item):
    """Resolve one chain trigger without re-entering the legacy BOM walker."""
    from gamedata import DEFAULT_RECORD_STORE, ability_graph

    guid = str(item.get("ability_guid") or "").lower()
    if not guid:
        return ""
    graph = ability_graph(DEFAULT_RECORD_STORE, guid)
    if graph is None:
        raise RuntimeError(f"ability {guid} is missing from current Records")
    source_uid = item.get("source_uid")
    owner_id = item.get("source_owner_uid")
    if owner_id is None and source_uid is not None:
        from pvp_db import db_card_owner_id
        owner_id = db_card_owner_id(
            session.session_id, int(source_uid), conn=db)
    if owner_id is None:
        owner_id = 0
        pchamp = getattr(handler, "_player_champ_scid", None)
        achamp = getattr(handler, "_ai_champ_scid", None)
        if pchamp is not None and source_uid is not None and int(
                pchamp.uid.uid64) == int(source_uid):
            owner_id = handler.user_profile["id"] if handler.user_profile else 0
        elif achamp is not None and source_uid is not None and int(
                achamp.uid.uid64) == int(source_uid):
            owner_id = 0
    activated = item.get("activated_ability_guid")
    if activated:
        battle_state["card_activated_item"] = {
            "kind": "ability", "ability_guid": activated,
            "source_uid": item.get("activated_source_uid"),
            "target_uid": item.get("activated_target_uid"),
            "instance_id": item.get("activated_ability_instance_id"),
            "activation_data": item.get("activated_activation_data") or {},
        }
    # ``target_uid`` is the card chosen for the authored input-bearing target
    # (a player picker, or the AI's legal pool), while ``trigger_target_uid``
    # is the card that raised the event.  They differ whenever a triggered
    # ability chooses "another" card: preferring the event card resolved the
    # effect against the trigger's own source (Armitron's Deploy buffed
    # Armitron instead of the selected Robot).  Keep the event card only for
    # the templates that name it, such as AbilityTriggerCardTargetTemplate.
    selected_target = item.get("target_uid")
    # Keep the authored event target distinct from the card selected for this
    # trigger's effect target. C# TriggerTargetPropertyVariable reads the
    # triggering event, even when the ability also selected another card.
    trigger_target = item.get("trigger_target_uid")
    activation_data = item.get("activation_data") or {}
    target_map = (dict(activation_data.get("target_map") or {})
                  if isinstance(activation_data, dict) else {})
    if not target_map and selected_target is not None:
        for index, spec in enumerate(graph.targets):
            if spec.requires_input:
                target_map[index] = int(selected_target)
                break
    old_source = battle_state.get("resolving_source_uid")
    old_owner = battle_state.get("resolving_owner_id")
    old_target = battle_state.get("resolving_target_uid")
    old_trigger_source = battle_state.get("resolving_trigger_source_uid")
    old_trigger_target = battle_state.get("resolving_trigger_target_uid")
    old_event_type = battle_state.get("resolving_trigger_event_type")
    old_event_data = battle_state.get("resolving_trigger_event_data")
    battle_state["resolving_source_uid"] = source_uid
    battle_state["resolving_owner_id"] = owner_id
    # The legacy trigger boundary exposed the trigger's selected card as the
    # transient target so an #TRIGGER_SOURCE#/#TRIGGER_TARGET# template whose
    # event class has no TargetCardId could still resolve the card that raised
    # it.  Publish the same alias for the remainder of this resolution.
    if selected_target is None:
        battle_state.pop("resolving_target_uid", None)
    else:
        battle_state["resolving_target_uid"] = int(selected_target)
    trigger_source = item.get("trigger_source_uid")
    if trigger_source is None:
        battle_state.pop("resolving_trigger_source_uid", None)
    else:
        battle_state["resolving_trigger_source_uid"] = int(trigger_source)
    # C# keeps the triggering event on the ability instance, so a target
    # template of kind AbilityTriggerCardTargetTemplate ("TriggerSource")
    # still resolves the event's card when the trigger waits on the chain for
    # priority instead of resolving inline.
    if trigger_target is None:
        battle_state.pop("resolving_trigger_target_uid", None)
    else:
        battle_state["resolving_trigger_target_uid"] = int(trigger_target)
    battle_state["resolving_trigger_event_type"] = str(
        item.get("trigger_event_type") or "")
    event_data = item.get("trigger_event_data") or {}
    battle_state["resolving_trigger_event_data"] = (
        dict(event_data) if isinstance(event_data, dict) else {})
    try:
        state = resolve_port_ability(
            handler, game, session, db, player_uid, ai_uid, battle_state,
            guid, source_uid, owner_id, target_map=target_map,
            instance_id=int(item.get("instance_id", 1)))
        if not battle_state.get("resolution_paused"):
            # A queued trigger resolves here (mulligan/setup drains, the port's
            # chain resolver, and the PvP projections).  Consume an authored
            # ONE-SHOT once its effect has applied, exactly like the inline
            # trigger path, so the card loses the used ability on both sides.
            from .triggers import consume_one_shot_trigger
            consume_one_shot_trigger(
                handler, session, game, db, player_uid, ai_uid, battle_state,
                guid, source_uid)
        return state.name if hasattr(state, "name") else str(state)
    finally:
        battle_state["resolving_source_uid"] = old_source
        battle_state["resolving_owner_id"] = old_owner
        if old_target is None:
            battle_state.pop("resolving_target_uid", None)
        else:
            battle_state["resolving_target_uid"] = old_target
        if old_trigger_source is None:
            battle_state.pop("resolving_trigger_source_uid", None)
        else:
            battle_state["resolving_trigger_source_uid"] = old_trigger_source
        if old_trigger_target is None:
            battle_state.pop("resolving_trigger_target_uid", None)
        else:
            battle_state["resolving_trigger_target_uid"] = old_trigger_target
        if old_event_type is None:
            battle_state.pop("resolving_trigger_event_type", None)
        else:
            battle_state["resolving_trigger_event_type"] = old_event_type
        if old_event_data is None:
            battle_state.pop("resolving_trigger_event_data", None)
        else:
            battle_state["resolving_trigger_event_data"] = old_event_data
