"""RulesPort-owned ability resolution boundary."""

from __future__ import annotations

from .actions import AbilityResolutionState


def _random_target_sample(candidates, count, battle_state):
    """Port of ``AbilityTargetTemplate.FilterRandomTargets``.

    A random auto-target resolves from the full legal pool but the effect
    applies to at most ``count`` cards chosen with the session RNG.  The C#
    client does a partial Fisher-Yates: for ``i`` from ``n`` down to
    ``n - count + 1`` it picks ``rng.Next(i)``, swaps that slot with slot
    ``i-1`` and keeps the picked card.  Reproducing the swap (rather than a
    plain ``pop``) keeps the RNG call sequence and pool state identical to the
    client for replay parity.
    """
    pool = list(candidates)
    total = len(pool)
    count = max(1, int(count or 1))
    if total <= count:
        return tuple(pool)
    rng = (battle_state or {}).get("_rules_rng")
    if rng is not None and hasattr(rng, "next"):
        picked = []
        for i in range(total, total - count, -1):
            index = int(rng.next(i)) % i
            picked.append(pool[index])
            pool[index] = pool[i - 1]
        return tuple(picked)
    import random
    return tuple(random.sample(pool, count))


class NativeEffectBackend:
    """Walk one typed ability without entering the legacy BOM resolver."""

    def __call__(self, *, handler, game, session, db, player_uid, ai_uid,
                 battle_state, ability, resume_from_order=None,
                 native_effect=None, effect_groups=None):
        if native_effect is None:
            from .effects import dispatch
            native_effect = dispatch
        from rules_port.context import EffectContext
        from rules_port.conditions import ConditionContext, evaluate_effect_condition

        old = {key: battle_state.get(key) for key in (
            "resolving_ability", "resolving_source_uid",
            "resolving_owner_id", "resolving_target_uid",
            "resolving_responsible_player_id", "resolving_effect_order",
            "ability_target_map", "_rules_port_native_effect")}
        battle_state["resolving_ability"] = ability.ability_template_id
        battle_state["resolving_source_uid"] = ability.source_uid
        battle_state["resolving_owner_id"] = int(
            ability.responsible_player_id or 0)
        battle_state["resolving_responsible_player_id"] = int(
            ability.responsible_player_id or 0)
        battle_state["ability_target_map"] = dict(
            getattr(ability.activation, "target_map", {}) or {})
        from .targeting import legal_targets
        battle_state["_rules_port_native_effect"] = True
        applied = battle_state.setdefault("applied_effects", {})
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
        try:
            effects = ability.ordered_effects
            start = int(resume_from_order or 0)
            allowed_groups = (None if effect_groups is None else
                              {int(group) for group in effect_groups})
            for position, effect in enumerate(effects):
                if position < start:
                    continue
                battle_state["resolving_effect_order"] = position
                effect_guid = str(field(effect, "guid", "")).lower()
                effect_type = str(field(effect, "concrete_type", ""))
                effect_group = int(field(effect, "effect_group_id", 0))
                if allowed_groups is not None and effect_group not in allowed_groups:
                    continue
                target_index = int(field(effect, "target_index", -1))
                instance_id = int(field(effect, "effect_instance_id", position))
                contingent = int(field(
                    effect, "contingent_effect_instance_id", -1))
                if contingent >= 0 and not applied.get(contingent, False):
                    applied[instance_id] = False
                    continue
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
                            # Player targets are typed identities, not card
                            # filters.  Do not send a null card-filter spec
                            # through the card-target evaluator (common for
                            # "You" targets such as token summons).
                            target_values = (ability.responsible_player_id,)
                        elif (int(ability.responsible_player_id or 0) == 0 and
                              kind.endswith("AbilityTargetTemplate")):
                            # Server-driven AI activations still need the
                            # same authored target pool as a client picker.
                            # Choose deterministically from native legal
                            # targets; do not fall back to the parent source.
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
                            if target_spec.is_random:
                                candidates = _random_target_sample(
                                    candidates,
                                    max(1, int(target_spec.maximum or 1)),
                                    battle_state)
                                target_values = candidates
                            else:
                                maximum = int(target_spec.maximum or 0)
                                target_values = (candidates[:maximum]
                                                 if maximum > 0 else candidates[:1])
                        elif kind in ("AbilityTriggerCardTargetTemplate",
                                      "SourceDrawnTargetTemplate",
                                      "SourceBuriedTargetTemplate"):
                            target_values = (battle_state.get(
                                "resolving_trigger_target_uid"),)
                        elif target_spec.is_auto:
                            both_players = str(target_spec.player_filter or "").lower() in (
                                "multipleplayers", "allplayers")
                            candidates = tuple(legal_targets(
                                db, session.session_id,
                                int(ability.responsible_player_id or 0),
                                target_spec.guid, ability.source_uid,
                                both_players=both_players,
                                champions=(getattr(handler, "_champion_targets",
                                                   lambda: [])() or []),
                                battle_state=battle_state))
                            if target_spec.is_random:
                                # A random auto-target (e.g. Infernal Professor's
                                # "a random non-resource card from your deck")
                                # must resolve to a bounded random sample, not
                                # every legal card.  Iterating the whole pool
                                # moved the entire deck into hand.
                                candidates = _random_target_sample(
                                    candidates,
                                    max(1, int(target_spec.maximum or 1)),
                                    battle_state)
                            elif int(target_spec.maximum or 0) > 0 and \
                                    len(candidates) > int(target_spec.maximum):
                                # C# GetAutoTargets truncates a non-random
                                # auto-target to GetMaximumTargetCount.
                                candidates = candidates[:int(target_spec.maximum)]
                            target_values = candidates
                        elif target_spec.target_kind == "SourceRevealedTargetTemplate":
                            from .targeting import revealed_target_uids
                            candidates = revealed_target_uids(
                                db, session.session_id,
                                ability.responsible_player_id, ability.source_uid,
                                target_spec.guid,
                                battle_state.get("revealed_cards") or [],
                                battle_state=battle_state)
                            if candidates and int(ability.responsible_player_id or 0) != 0:
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
                                        optional=bool(target_spec.optional),
                                        continuation=continuation)
                                    battle_state["resolution_paused"] = True
                                    native_waiting = True
                            else:
                                target_values = tuple(candidates[:max(
                                    1, int(target_spec.maximum or 1))])
                if native_waiting:
                    break
                if not target_values:
                    target_values = (None,)
                if not isinstance(target_values, (tuple, list)):
                    target_values = (target_values,)
                effect_param = field(effect, "param", "")
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
                        ability=ability)
                    condition_id = str(field(effect, "condition_guid", ""))
                    if condition_id and condition_id != "0" * 36:
                        condition_context = ConditionContext(
                            db, session, battle_state,
                            event_type="AbilityEffectEvent",
                            ability_source_uid=ability.source_uid,
                            ability_source_owner_id=ability.responsible_player_id,
                            trigger_uid=target,
                            pl_t=player_uid, ai_t=ai_uid,
                            event_int_attribute=None)
                        if not evaluate_effect_condition(
                                db, condition_id, condition_context):
                            applied[instance_id] = False
                            continue
                    result = native_effect(effect_type, effect_context, effect)
                    if result is None:
                        raise RuntimeError(
                            "RulesPort native effect has no handler: "
                            f"{effect_type} ({effect_guid})")
                    applied[instance_id] = True
                    battle_state.setdefault("rules_port_effect_results", []).append(
                        {"instance_id": instance_id, "result": str(result)})
                if battle_state.get("resolution_paused"):
                    battle_state["rules_port_resume_effect_order"] = position
                    break
        finally:
            for key, value in old.items():
                if value is None:
                    battle_state.pop(key, None)
                else:
                    battle_state[key] = value


class PortAbilityResolver:
    """Resolve one port-owned ability and persist its UI continuation."""

    def __init__(self, handler, game, game_session, db, player_uid, ai_uid,
                 battle_state: dict, *, native_effect=None,
                 effect_groups=None) -> None:
        self.handler = handler
        self.game = game
        self.game_session = game_session
        self.db = db
        self.player_uid = player_uid
        self.ai_uid = ai_uid
        self.battle_state = battle_state
        self.effect_groups = effect_groups
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
                effect_groups=self.effect_groups)
        finally:
            if previous_strict is None:
                self.battle_state.pop("_rules_port_strict_effects", None)
            else:
                self.battle_state["_rules_port_strict_effects"] = previous_strict
        port_session = getattr(self.game_session, "_rules_port_session", None)
        if self.battle_state.get("pending_discard_ability") and port_session is not None:
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
                       target_map=None, variables=None):
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
    ability.bind_activation({"target_map": target_map or {},
                             "variables": variables or {}})
    return ability


def resolve_port_ability(handler, game, session, db, player_uid, ai_uid,
                         battle_state, ability_guid, source_uid, owner_id,
                         *, target_map=None, variables=None,
                         resume_from_order=None, instance_id=1,
                         native_effect=None,
                         effect_groups=None):
    """Resolve a persisted continuation through the port-owned lifecycle."""
    ability = build_port_ability(
        ability_guid, source_uid, owner_id, instance_id=instance_id,
        target_map=target_map, variables=variables)
    # The resume offset is persisted in the existing session snapshot because
    # the continuation may have crossed a reconnect boundary.
    if resume_from_order is not None:
        battle_state["rules_port_resume_effect_order"] = int(resume_from_order)
    else:
        # A nested child is a fresh ability. Do not let it inherit the parent
        # continuation offset from the shared battle-state dictionary.
        battle_state.pop("rules_port_resume_effect_order", None)
    return PortAbilityResolver(
        handler, game, session, db, player_uid, ai_uid, battle_state,
        native_effect=native_effect,
        effect_groups=effect_groups)(ability)


def resolve_port_played_spell(game, session, db, handler, player_uid, ai_uid,
                               battle_state, ability_guids, *,
                               activations=None):
    """Resolve all authored abilities on a spell through the port lifecycle.

    Card play is a multi-ability activation, not a special legacy resolver.
    Each graph is still sourced from Records, while scheduling, continuations,
    and effect dispatch remain RulesPort-owned.
    """
    from gamedata import ActivationData, DEFAULT_RECORD_STORE, ability_graph
    from .tac import tac_int

    bstate = battle_state or {}
    target_uid = bstate.get("player_spell_target")
    source_uid = bstate.get("resolving_source_uid")
    owner_id = bstate.get("resolving_owner_id")
    if owner_id is None and source_uid is not None:
        from pvp_db import db_card_owner_id
        owner_id = db_card_owner_id(
            session.session_id, int(source_uid), conn=db)
    if owner_id is None:
        owner_id = handler.user_profile["id"] if handler.user_profile else 0

    activation_map = {
        str(key).lower(): ActivationData.from_dict(value)
        for key, value in (activations or {}).items()
    }
    previous = bstate.get("_esc_counted_this_resolution")
    bstate["_esc_counted_this_resolution"] = False
    logs = []
    try:
        for instance_id, guid_value in enumerate(ability_guids or [], 1):
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
                for index, target in enumerate(graph.targets):
                    if target.requires_input:
                        target_map[index] = int(target_uid)
                        break
            state = resolve_port_ability(
                handler, game, session, db, player_uid, ai_uid, bstate,
                guid, source_uid, owner_id, target_map=target_map,
                variables=(activation.variables if activation is not None
                           else None), instance_id=instance_id)
            logs.append(state.name if hasattr(state, "name") else state)
            if bstate.get("resolution_paused"):
                break
    finally:
        if previous is None:
            bstate.pop("_esc_counted_this_resolution", None)
        else:
            bstate["_esc_counted_this_resolution"] = previous
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
        }
    target = item.get("trigger_target_uid", item.get("target_uid"))
    target_map = {}
    if target is not None:
        for index, spec in enumerate(graph.targets):
            if spec.requires_input:
                target_map[index] = int(target)
                break
    old_source = battle_state.get("resolving_source_uid")
    old_owner = battle_state.get("resolving_owner_id")
    battle_state["resolving_source_uid"] = source_uid
    battle_state["resolving_owner_id"] = owner_id
    try:
        state = resolve_port_ability(
            handler, game, session, db, player_uid, ai_uid, battle_state,
            guid, source_uid, owner_id, target_map=target_map,
            instance_id=int(item.get("instance_id", 1)))
        return state.name if hasattr(state, "name") else str(state)
    finally:
        battle_state["resolving_source_uid"] = old_source
        battle_state["resolving_owner_id"] = old_owner
