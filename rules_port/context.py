"""Context and small operations shared by metadata effect executors.

The resolver still accepts the historical leaf ABI internally.  New leaves can
use :class:`EffectContext` instead, which keeps the protocol/session plumbing,
typed fields, target ownership, and common operations in one place.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
from typing import Any


@dataclass
class EffectContext:
    """Execution context for one resolved effect target.

    ``bstate`` remains the authoritative short-lived resolver state.  This
    class is deliberately an adapter over it, not a replacement state store.
    That lets existing leaves and context-style leaves execute in the same
    resolver during the migration.
    """

    game: Any
    session: Any
    db: Any
    handler: Any
    player_uid: Any
    ai_uid: Any
    bstate: dict[str, Any]
    effect_guid: str
    param: str = ""
    # This is deliberately an execution-only dependency.  ``bstate`` may be
    # persisted by an effect (draw is one such path), so placing the builder
    # there makes a normal resolution non-serializable.
    runtime_ability: Any = None
    native_context: bool = False

    @classmethod
    def from_legacy(cls, game, session, db, handler, pl_t, ai_t, bstate,
                    effect_guid, param="", *, ability=None) -> "EffectContext":
        return cls(
            game=game,
            session=session,
            db=db,
            handler=handler,
            player_uid=pl_t,
            ai_uid=ai_t,
            bstate=bstate if bstate is not None else {},
            effect_guid=effect_guid,
            param=param or "",
            runtime_ability=ability,
            native_context=False,
        )

    @classmethod
    def from_rules_port(cls, game, session, db, handler, pl_t, ai_t,
                        bstate, effect_guid, param="", *, ability=None):
        """Construct the shared mutation context for native RulesPort work.

        The context is a host/projection seam, not a second rules resolver.
        Keeping a distinct constructor prevents native code from advertising
        that it is entering through the historical leaf ABI.
        """
        return cls(
            game=game, session=session, db=db, handler=handler,
            player_uid=pl_t, ai_uid=ai_t,
            bstate=bstate if bstate is not None else {},
            effect_guid=effect_guid, param=param or "", runtime_ability=ability,
            native_context=True)

    @property
    def ability_guid(self) -> str:
        return str(self.bstate.get("resolving_ability") or "").lower()

    @property
    def ability(self):
        """Return the active metadata-backed builder, when resolving.

        The builder is intentionally carried by this transient context rather
        than ``bstate``.  Resolver state crosses persistence boundaries while
        an effect is running and must consequently remain JSON serializable.
        """
        return self.runtime_ability

    def target(self, name: str = "primary", default: int | None = None):
        """Return the current typed target without inventing one.

        The resolver invokes a leaf once per target and records the target in
        ``resolving_target_uid``.  Falling back to the source here would make
        an explicitly empty target accidentally affect the source card.
        """
        key = str(name or "primary").lower()
        if key in ("source", "self"):
            value = self.bstate.get("resolving_source_uid")
        elif key in ("secondary", "secondary_target"):
            value = self.bstate.get("resolving_secondary_target_uid")
        else:
            value = self.bstate.get("resolving_target_uid")
        if value is None:
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def resolved_target(self, default: int | None = None):
        """Return the legacy BOM target precedence through one typed helper.

        Ordinary resolver leaves use ``resolving_target_uid``.  Older spell,
        modifier, and trigger paths still populate the two named aliases, and
        source-target effects intentionally fall back to the source card.  A
        simple leaf should not have to know those storage details.
        """
        for key in ("player_spell_target", "player_mod_target",
                    "resolving_target_uid", "resolving_source_uid"):
            value = self.bstate.get(key)
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        return default

    def _emit_trigger(self, event_type, source_card_id, source_owner_id=None,
                      target_card_id=None, **data):
        """Emit one event through the active RulesPort trigger boundary."""
        if self.native_context or self.bstate.get("_rules_port_native_effect"):
            from rules_port.triggers import dispatch_trigger
            return dispatch_trigger(
                self, str(event_type).rsplit(".", 1)[-1], source_card_id,
                source_owner_id, target_card_id, data=data)
        from abilities.framework.triggers import resolve_triggers
        return resolve_triggers(
            self.db, self.handler, self.game, self.session,
            self.player_uid, self.ai_uid, self.bstate,
            str(event_type).rsplit(".", 1)[-1], source_card_id,
            source_owner_uid=source_owner_id,
            extra_target=target_card_id,
            event_source_collection=data.get("event_source_collection"),
            event_destination_collection=data.get("event_destination_collection"),
            event_previous_state=data.get("event_previous_state"),
            event_int_attribute=data.get("event_int_attribute"),
            event_tac=dict(data.get("event_tac") or {}))

    def modifier_target(self, default: int | None = None):
        """Return the target selected for a typed card modifier.

        The resolver's current target is authoritative. The two older
        aliases are retained only for direct legacy callers and fixtures that
        invoke a leaf outside the normal resolution loop.
        """
        target = self.target(default=None)
        if target is not None:
            return target
        for key in ("player_mod_target", "player_spell_target"):
            value = self.bstate.get(key)
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        return default


    def value(self, field_name: str, default: int = 0) -> int:
        """Evaluate a typed effect field against the active ability state."""
        if self.ability is not None:
            return self.ability.value(
                self.db, self.bstate, field_name,
                effect=self.effect_guid, default=default)
        if self.native_context or self.bstate.get("_rules_port_native_effect"):
            from rules_port.fields import effect_field
            return effect_field(
                self.ability, self.db, self.bstate, self.effect_guid,
                field_name, default)
        from abilities.framework.fields import effect_field

        return effect_field(
            self.db, self.bstate, self.effect_guid, field_name, default)

    def template_value(self, field_name: str, default: Any = None) -> Any:
        """Read a non-numeric typed field from the effect template."""
        if self.ability is not None:
            return self.ability.template_value(
                self.db, self.bstate, field_name,
                effect=self.effect_guid, default=default)
        if self.native_context or self.bstate.get("_rules_port_native_effect"):
            from rules_port.fields import effect_template_value
            return effect_template_value(
                self.ability, self.effect_guid, field_name, default)
        from abilities.framework.fields import effect_template_value

        return effect_template_value(
            self.db, self.bstate, self.effect_guid, field_name, default)

    def randomize_variable(self) -> str:
        """Roll a typed random variable into the active ability state."""
        import random

        template = self.template_value("m_VariableName", "RandomNumber")
        name = str(template or "RandomNumber")
        minimum = self.template_value("m_MinValue", 1)
        maximum = self.template_value("m_MaxValue", minimum)
        maximum_field = self.template_value("m_MaxValueField")
        if maximum_field:
            from rules_port.fields import resolve_field

            variables = dict(self.bstate.get("ability_variables") or {})
            maximum = resolve_field(
                maximum_field, variables, self.bstate.get("effect_outputs") or
                {}, self.bstate, maximum)
        try:
            minimum = int(minimum or 0)
        except (TypeError, ValueError):
            minimum = 0
        try:
            maximum = int(maximum or minimum)
        except (TypeError, ValueError):
            maximum = minimum
        value = random.randint(minimum, max(minimum, maximum))
        self.bstate.setdefault("ability_variables", {})[name] = value
        return f"randomized {name}={value}"

    def target_owner(self, target: int | None = None,
                     default: int | None = None) -> int | None:
        """Resolve a card/champion target to its controlling player."""
        target = self.target() if target is None else target
        if target is None:
            return default
        from pvp_db import db_card_owner_id
        card_owner = db_card_owner_id(
            self.session.session_id, int(target), conn=self.db)
        if card_owner is not None:
            return int(card_owner)
        for pid, champion_uid in (self.bstate.get("champ_map") or {}).items():
            try:
                if int(champion_uid) == int(target):
                    return int(pid)
            except (TypeError, ValueError):
                continue
        profile = getattr(self.handler, "user_profile", None)
        player_owner = profile.get("id", 0) if isinstance(profile, dict) else 0
        for attr, owner in (("_player_champ_scid", player_owner),
                            ("_ai_champ_scid", 0)):
            champion = getattr(self.handler, attr, None)
            if champion is None:
                continue
            try:
                if int(champion.uid.uid64) == int(target):
                    return int(owner)
            except (AttributeError, TypeError, ValueError):
                continue
        return default

    def draw(self, count: int, owner: int | None = None) -> str:
        """Draw cards through the existing PvE/PvP handler boundary."""
        count = max(0, int(count))
        target_owner = self.target_owner(default=None)
        if owner is None:
            owner = target_owner
        if self.native_context or self.bstate.get("_rules_port_native_effect"):
            from rules_port.draw_effects import draw_cards
            return draw_cards(self, count, owner=owner)
        for _ in range(count):
            if owner == 0:
                import ai
                ai.ai_draw_card(self.handler, self.game, self.session,
                                self.ai_uid, self.bstate)
                continue
            from rules_port.runtime_helpers import owner_uid

            draw_uid = (self.player_uid if owner is None else owner_uid(
                owner, self.player_uid, self.ai_uid, self.bstate))
            draw_fn = self.handler._player_draw_card
            try:
                accepts_owner = len(inspect.signature(draw_fn).parameters) >= 4
            except (TypeError, ValueError):
                accepts_owner = True
            if accepts_owner:
                draw_fn(self.game, self.session, draw_uid, owner)
            else:
                draw_fn(self.game, self.session, draw_uid)
        return f"draw {count} for owner {owner}"

    def draw_effect(self) -> str:
        """Draw using the effect's typed count and resolved target owner."""
        count = self.value("m_InputValue", default=1)
        # Synthetic compatibility fixtures may provide only the compact
        # adapter payload. Live Records-backed effects always use the typed
        # field above.
        if self.template_value("m_InputValue") is None and self.param:
            try:
                payload = json.loads(self.param)
                if isinstance(payload, dict) and "count" in payload:
                    count = int(payload["count"])
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        return self.draw(
            count, owner=self.target_owner(default=None))

    def conversation(self) -> str:
        """Queue the typed encounter conversation and pause this BOM.

        ConversationAbilityEffectTemplate is a protocol boundary: the
        original client opens a UI animation and sends a transaction before
        the remaining effects may resolve.  The battle handler owns the
        persisted continuation and wire event; this context only supplies the
        typed conversation ID and current ability state.
        """
        conversation_id = self.template_value("m_ConversationId", "")
        if isinstance(conversation_id, dict):
            conversation_id = conversation_id.get("m_Guid", "")
        conversation_id = str(conversation_id or "").lower()
        if not conversation_id or conversation_id == "0" * 8 + "-" + "0" * 4 + "-" + "0" * 4 + "-" + "0" * 4 + "-" + "0" * 12:
            return "conversation: invalid id"
        queue = getattr(self.handler, "_queue_conversation_prompt", None)
        if callable(queue):
            return queue(
                self.game, self.session, self.player_uid, self.ai_uid,
                self.bstate, conversation_id,
            )
        # Headless adapters may not expose a wire handler. Preserve the same
        # continuation marker so tests can assert that the effect pauses
        # instead of silently succeeding.
        pending = self.continuation()
        pending.update({"kind": "conversation", "conversation_id": conversation_id})
        self.bstate["pending_conversation"] = pending
        self.bstate["resolution_paused"] = True
        return f"conversation: awaiting {conversation_id}"

    def continuation(self, *, resume_effect_order: int | None = None,
                     **overrides) -> dict[str, Any]:
        """Return a JSON-safe snapshot for an authored pause/resume point."""
        if self.native_context:
            from rules_port.abilities import AbilityContinuation
        else:
            from abilities.framework.builder import AbilityContinuation

        if resume_effect_order is None:
            resume_effect_order = int(
                self.bstate.get("resolving_effect_order", 0) or 0) + 1
        if self.ability is not None:
            value = self.ability.continuation(
                resume_effect_order=resume_effect_order,
                source_uid=overrides.pop(
                    "source_uid", self.bstate.get("resolving_source_uid")),
                owner_id=overrides.pop(
                    "owner_id", self.bstate.get("resolving_owner_id", 0)),
                target_map=overrides.pop(
                    "target_map", self.bstate.get("ability_target_map") or {}),
                variables=overrides.pop(
                    "variables", self.bstate.get("ability_variables") or {}))
        else:
            value = AbilityContinuation.from_state(
                self.bstate, resume_effect_order=resume_effect_order,
                **{key: overrides.pop(key) for key in (
                    "ability_guid", "source_uid", "owner_id", "target_map",
                    "variables") if key in overrides}).to_dict()
        value.update(overrides)
        return value

    def put_top_into_hand(self) -> str:
        """Put typed-count deck cards into the caster's hand."""
        import game_engine

        from rules_port.runtime_helpers import owner_uid

        count = self.value("m_InputValue", default=1)
        target = self.resolved_target()
        if self.native_context or self.bstate.get("_rules_port_native_effect"):
            deck_owner = self.target_owner(
                target, default=self.bstate.get("resolving_owner_id", 0))
        else:
            from abilities.framework.bom import _deck_owner_for_target
            deck_owner = (_deck_owner_for_target(
                self.db, self.handler, self.session, self.bstate, target)
                if target is not None else None)
        if deck_owner is None:
            deck_owner = 0
        if self.bstate.get("pvp"):
            hand_owner = int(self.player_uid.uid64) >> 8
        else:
            profile = getattr(self.handler, "user_profile", None) or {}
            hand_owner = int(profile.get("id", 0))
        moved = 0
        for _ in range(max(0, int(count))):
            from pvp_db import db_deck_top_card_details, db_draw_card_to_hand
            row = db_deck_top_card_details(
                self.session.session_id, deck_owner, conn=self.db)
            if not row:
                break
            scid = game_engine.SessionCardId(game_engine.UID(row[1]))
            db_draw_card_to_hand(
                self.session.session_id, row[0], hand_owner, conn=self.db)
            self.db.commit()
            tpl_guid, ct, _name, cost, atk, defense, gem = \
                self.handler._card_full_data(
                    self.game, scid, row[3], row[2])
            owner = owner_uid(hand_owner, self.player_uid, self.ai_uid,
                              self.bstate)
            self.game.push_card_moved(
                scid, owner, game_engine.ECardCollections.Hand,
                game_engine.ECardLocations.Top, 1)
            self.game.push_card_drawn(scid, owner, 1)
            self.game.push_card_updated(
                scid, owner, game_engine.ECardCollections.Hand, ct,
                attack=atk, defense=defense, cost=cost,
                template_id=tpl_guid, gems=gem)
            moved += 1
        return f"put {moved} deck card(s) into hand"

    def bury(self) -> str:
        """Move the typed number of cards from a deck into its discard."""
        import game_engine

        from pvp_db import db_discard_card
        from rules_port.runtime_helpers import owner_uid
        count = self.value("m_Amount", default=1)
        target = self.resolved_target()
        if self.native_context or self.bstate.get("_rules_port_native_effect"):
            deck_owner = self.target_owner(
                target, default=self.bstate.get("resolving_owner_id", 0))
        else:
            from abilities.framework.bom import _deck_owner_for_target
            deck_owner = (_deck_owner_for_target(
                self.db, self.handler, self.session, self.bstate, target)
                if target is not None else None)
        if deck_owner is None:
            deck_owner = 0
        discard_owner = owner_uid(deck_owner, self.player_uid, self.ai_uid,
                                  self.bstate)
        total = 0
        for _ in range(max(0, int(count))):
            from pvp_db import db_deck_top_card_details
            top = db_deck_top_card_details(
                self.session.session_id, deck_owner, conn=self.db)
            row = (top[1], top[3]) if top else None
            if not row:
                break
            scid = game_engine.SessionCardId(game_engine.UID(row[0]))
            db_discard_card(self.session.session_id, row[0],
                            connection=self.db)
            _tpl, ct, _name, cost, atk, defense, gem = \
                self.handler._card_full_data(
                    self.game, scid, row[1])
            self.game.push_card_updated(
                scid, discard_owner, game_engine.ECardCollections.Discard,
                ct, template_id=row[1], attack=atk, defense=defense,
                cost=cost, gems=gem)
            self.game.push_card_moved(
                scid, discard_owner, game_engine.ECardCollections.Discard,
                game_engine.ECardLocations.Top, 1)
            self._emit_trigger(
                "CardEnteredZoneEvent", int(row[0]), deck_owner,
                event_source_collection="deck",
                event_destination_collection="discard",
                event_previous_state=0)
            self._emit_trigger(
                "CardDiscardedEvent", int(row[0]), deck_owner,
                event_source_collection="deck",
                event_destination_collection="discard",
                event_previous_state=0)
            total += 1
        return f"bury {total} cards"

    def void_card(self) -> str:
        """Move a resolved card to Void and publish its exit relationship."""
        import game_engine

        from rules_port.runtime_helpers import owner_uid

        target = self.target()
        if target is None:
            target = self.resolved_target()
        if target is None:
            return "void: no target"
        target = int(target)
        from pvp_db import db_card_owner_id, db_card_zone_details, db_void_card
        card_owner = db_card_owner_id(
            self.session.session_id, target, conn=self.db)
        if card_owner is None:
            return "void: target not found"
        owner = owner_uid(card_owner, self.player_uid, self.ai_uid, self.bstate)
        db_void_card(self.session.session_id, target, conn=self.db)
        self.db.commit()
        scid = game_engine.SessionCardId(game_engine.UID(target))
        details = db_card_zone_details(
            self.session.session_id, target, conn=self.db)
        tpl_guid = details[0] if details else None
        _tpl, ct, _name, cost, atk, defense, gem = \
            self.handler._card_full_data(self.game, scid, tpl_guid)
        self.game.push_card_moved(
            scid, owner, game_engine.ECardCollections.Void,
            game_engine.ECardLocations.Top, 0)
        self.game.push_card_updated(
            scid, owner, game_engine.ECardCollections.Void, ct,
            template_id=tpl_guid, attack=atk, defense=defense,
            cost=cost, gems=gem)
        self._emit_trigger("CardExitedZoneEvent", target, card_owner)

        source = self.bstate.get("resolving_source_uid")
        if source is not None:
            self.bstate.setdefault("voided_by", {}).setdefault(
                str(int(source)), []).append(target)
            source_details = db_card_zone_details(
                self.session.session_id, int(source), conn=self.db)
            source_row = ((source_details[0], source_details[2], source_details[3])
                          if source_details else None)
            if source_row and source_row[2] == "warzone":
                source_scid = game_engine.SessionCardId(
                    game_engine.UID(int(source)))
                _tpl2, ct2, _name2, cost2, atk2, defense2, gem2 = \
                    self.handler._card_full_data(
                        self.game, source_scid, source_row[0])
                source_owner = owner_uid(
                    source_row[1], self.player_uid, self.ai_uid, self.bstate)
                self.game.push_card_updated(
                    source_scid, source_owner,
                    game_engine.ECardCollections.Warzone, ct2,
                    template_id=source_row[0], attack=atk2,
                    defense=defense2, cost=cost2, gems=gem2,
                    related_cards=[scid])
        return f"voided {hex(target)}"




    def damage(self, target: int | None, amount: int) -> str:
        """Apply damage using the shared damage/death/replacement path."""
        if target is None:
            return "damage: no target"
        if self.native_context or self.bstate.get("_rules_port_native_effect"):
            from rules_port.damage_effects import deal_damage
            return deal_damage(self, target, int(amount))
        from abilities.framework.effects.damage import deal_damage

        return deal_damage(
            self.game, self.session, self.db, self.handler,
            self.player_uid, self.ai_uid, self.bstate, target, int(amount))

    def lose_life(self, target: int | None, amount: int,
                  metadata: dict | None = None) -> str:
        """Apply a typed LoseLifeModifier directly to champion health.

        Hex distinguishes life loss from damage. Life loss updates health
        directly and does not raise CardWouldBeDamagedEvent, which prevents a
        replacement ability from replacing its own life-loss consequence.
        """
        if target is None:
            return "lose life: no target"
        import game_engine
        from rules_port.runtime_helpers import owner_uid

        owner = self.target_owner(
            target, default=self.bstate.get("resolving_owner_id", 0))
        owner = int(owner or 0)
        if self.bstate.get("pvp"):
            health_key = (self.bstate.get("pvp_health_map") or {}).get(
                owner, f"hp_{owner}")
        else:
            health_key = "player_health" if owner else "ai_health"
        current = int(self.bstate.get(
            health_key, getattr(self.game, health_key, 20)) or 0)
        if metadata and metadata.get("lose_half_health"):
            amount = current // 2
        amount = max(0, int(amount or 0))
        if not amount:
            return "lose life: 0"
        try:
            from rules_port.static_rules import global_flags
            if "cant_lose_health" in global_flags(
                    self.db, self.session.session_id, self.bstate):
                return "prevented: can't lose health"
        except Exception:
            pass
        new_value = max(0, current - amount)
        self.bstate[health_key] = new_value
        setattr(self.game, health_key, new_value)
        event = game_engine.ChampionHealthChangedSessionEventArgs()
        event.player_id = owner_uid(owner, self.player_uid, self.ai_uid,
                                    self.bstate)
        event.old_damage_value = current
        event.new_damage_value = new_value
        self.game._push(event)
        return f"lose {amount} health -> {new_value}"

    def _effect_owner(self, target: int | None = None) -> int:
        """Resolve the controller for a modifier, including untargeted ones."""
        owner = self.target_owner(target, default=None)
        if owner is not None:
            return int(owner)
        owner = self.bstate.get("resolving_owner_id")
        if owner is not None:
            return int(owner)
        source = self.bstate.get("resolving_source_uid")
        if source is not None:
            from pvp_db import db_card_owner_id
            card_owner = db_card_owner_id(
                self.session.session_id, int(source), conn=self.db)
            if card_owner is not None:
                return int(card_owner)
        profile = getattr(self.handler, "user_profile", None)
        return int(profile.get("id", 0)) if isinstance(profile, dict) else 0

    def _side_keys(self, owner: int) -> tuple[str, str]:
        """Return the FRA view keys for a controller's mutable resources."""
        if self.bstate.get("pvp"):
            health_key = (self.bstate.get("pvp_health_map") or {}).get(owner)
            if health_key == "ai_health":
                return "ai", "ai_health"
            return "player", "player_health"
        return ("ai", "ai_health") if int(owner) == 0 else (
            "player", "player_health")

    def set_hero_health(self, target: int | None, value: int) -> str:
        """Apply the typed SetHeroHealthModifier operation."""
        import game_engine
        from rules_port.runtime_helpers import owner_uid

        owner = self._effect_owner(target)
        _side, health_key = self._side_keys(owner)
        old_value = int(self.bstate.get(
            health_key, getattr(self.game, health_key, 20)) or 0)
        new_value = max(0, int(value or 0))
        self.bstate[health_key] = new_value
        setattr(self.game, health_key, new_value)
        if old_value == new_value:
            return f"set health {new_value} (unchanged)"
        event = game_engine.ChampionHealthChangedSessionEventArgs()
        event.player_id = owner_uid(owner, self.player_uid, self.ai_uid,
                                    self.bstate)
        event.old_damage_value = old_value
        event.new_damage_value = new_value
        self.game._push(event)
        return f"set health {old_value}->{new_value}"

    def spell_points(self, target: int | None, amount: int) -> str:
        """Apply the typed SpellPointsModifier to the target controller."""
        import game_engine
        from rules_port.runtime_helpers import owner_uid

        owner = self._effect_owner(target)
        side, _health_key = self._side_keys(owner)
        key = f"{side}_spell_points"
        current = int(self.bstate.get(key, getattr(self.game, key, 0)) or 0)
        new_value = max(0, current + int(amount or 0))
        self.bstate[key] = new_value
        setattr(self.game, key, new_value)
        delta = new_value - current
        if delta:
            event = game_engine.ChampionSpellPointsChangedSessionEventArgs()
            event.player_id = owner_uid(owner, self.player_uid, self.ai_uid,
                                        self.bstate)
            event.operation = 1 if delta > 0 else 2
            event.delta = abs(delta)
            event.new_value = new_value
            self.game._push(event)
        return f"spell points {current}->{new_value}"

    @staticmethod
    def _shard_flag(value) -> int:
        import game_engine

        if isinstance(value, int):
            return int(value)
        return int(game_engine.SHARD_TO_FLAG.get(
            str(value or "").lower(), 0) or 0)

    def _card_buffs(self, target: int, column: str = "permanent_buffs"):
        from pvp_db import db_card_mutation_field
        raw = db_card_mutation_field(
            self.session.session_id, int(target), column, conn=self.db)
        try:
            value = json.loads(raw or "{}") if raw else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            value = {}
        return value if isinstance(value, dict) else {}

    def _save_card_buffs(self, target: int, buffs: dict,
                         column: str = "permanent_buffs"):
        from pvp_db import db_set_card_mutation_field
        db_set_card_mutation_field(
            self.session.session_id, int(target), column,
            json.dumps(buffs), conn=self.db)
        self.db.commit()

    def _card_thresholds(self, target: int) -> list[int]:
        buffs = self._card_buffs(target)
        if isinstance(buffs.get("thresholds"), list):
            return [int(value) for value in buffs["thresholds"]]
        from pvp_db import db_card_template_threshold_subtype
        row = db_card_template_threshold_subtype(
            self.session.session_id, int(target), conn=self.db)
        from rules_port.targeting import shards_from_threshold
        return list(shards_from_threshold(row[0] if row else "{}"))

    def _push_modifier_card(self, target: int, **kwargs):
        """Refresh the client representation after an instance modifier."""
        import game_engine
        from rules_port.runtime_helpers import card_collection_for_location, owner_uid

        from pvp_db import db_card_zone_details
        details = db_card_zone_details(
            self.session.session_id, int(target), conn=self.db)
        row = ((details[0], details[2], details[3]) if details else None)
        if not row:
            return
        scid = game_engine.SessionCardId(game_engine.UID(int(target)))
        tpl, ctype, _name, cost, attack, defense, gem = \
            self.handler._card_full_data(self.game, scid, row[0])
        self.game.push_card_updated(
            scid, owner_uid(row[1], self.player_uid, self.ai_uid, self.bstate),
            card_collection_for_location(row[2]), ctype, template_id=tpl,
            cost=cost, attack=attack, defense=defense, gems=gem, **kwargs)

    def card_threshold(self, target: int | None,
                       metadata: dict | None = None) -> str:
        """Apply CardThresholdModifier using the client's threshold rules."""
        if target is None:
            return "card threshold: no target"
        target = int(target)
        metadata = metadata or {}
        current = self._card_thresholds(target)
        if metadata.get("copysourcecard"):
            source = self.bstate.get("resolving_source_uid")
            new_thresholds = self._card_thresholds(int(source)) \
                if source is not None else current
        else:
            shard = self._shard_flag(metadata.get("shard"))
            if not shard:
                new_thresholds = []
            elif metadata.get("setthresholds"):
                new_thresholds = [shard]
            elif current:
                new_thresholds = [shard] * len(current)
            else:
                new_thresholds = current
        buffs = self._card_buffs(target)
        buffs["thresholds"] = [int(value) for value in new_thresholds]
        self._save_card_buffs(target, buffs)
        self._push_modifier_card(
            target, thresholds=[int(value) for value in new_thresholds])
        return f"thresholds {hex(target)} -> {new_thresholds}"

    def subtype_modifier(self, target: int | None,
                         metadata: dict | None = None) -> str:
        """Apply SubTypeModifier and persist the current subtype string."""
        if target is None:
            return "subtype: no target"
        target = int(target)
        metadata = metadata or {}
        subtype = str(metadata.get("subtype") or "").strip()
        if not subtype:
            return "subtype: empty"
        buffs = self._card_buffs(target)
        current = buffs.get("subtype")
        if current is None:
            from pvp_db import db_card_template_threshold_subtype
            row = db_card_template_threshold_subtype(
                self.session.session_id, target, conn=self.db)
            current = row[1] if row else ""
        values = [value for value in str(current or "").split() if value]
        operation = str(metadata.get("operation") or "Set").lower()
        if operation == "add" and subtype not in values:
            values.append(subtype)
        elif operation == "remove":
            values = [value for value in values
                      if value.lower() != subtype.lower()]
        else:
            values = [subtype]
        buffs["subtype"] = " ".join(values)
        self._save_card_buffs(target, buffs)
        self._push_modifier_card(target, sub_type=buffs["subtype"])
        return f"subtype {hex(target)} -> {buffs['subtype']}"

    def damage_shield(self, target: int | None, amount: int,
                      metadata: dict | None = None) -> str:
        """Attach the client's one-shot/amount damage shield to a card."""
        if target is None:
            return "damage shield: no target"
        metadata = metadata or {}
        entry = {
            "amount": max(0, int(amount or 0)),
            "only_combat": bool(metadata.get("onlycombatdamage")),
            "one_shot": bool(metadata.get("oneshot")),
            "lasts_indefinitely": bool(metadata.get("lastsindefinitely")),
        }
        if metadata.get("onlypreventfromdamagedealer"):
            dealer = self.bstate.get("resolving_secondary_target_uid")
            if dealer is None and int(metadata.get(
                    "damagedealeradditionaltarget", 0) or 0) == -2:
                dealer = self.bstate.get("resolving_source_uid")
            if dealer is not None:
                entry["dealer"] = int(dealer)
        target = int(target)
        column = ("permanent_buffs" if entry["lasts_indefinitely"]
                  else "temporary_buffs")
        from pvp_db import db_card_mutation_field
        raw = db_card_mutation_field(
            self.session.session_id, target, column, conn=self.db)
        if raw is not None:
            buffs = self._card_buffs(target, column)
            buffs.setdefault("damage_shields", []).append(entry)
            self._save_card_buffs(target, buffs, column)
            self._push_modifier_card(target, damage_shield=True)
        else:
            shields = self.bstate.setdefault("damage_shields", {})
            shields.setdefault(str(target), []).append(entry)
        return f"damage shield {hex(target)} +{entry['amount']}"

    def rule_modifier(self, target: int | None, param: dict,
                      metadata: dict | None = None) -> str:
        """Persist typed damage/targeting/block rules on a card instance."""
        if target is None:
            return "rule modifier: no target"
        metadata = metadata or {}
        prop = str(metadata.get("property") or param.get("property") or "")
        duration = str(param.get("duration") or "")
        column = ("temporary_buffs" if duration in (
            "EndOfTurn", "BeginningOfOwnersTurn",
            "AfterCardsReadyOnPlayersTurn") else "permanent_buffs")
        buffs = self._card_buffs(int(target), column)
        rules = buffs.setdefault("rule_modifiers", [])
        rule = {"property": prop}
        if metadata.get("cardfilter") is not None:
            rule["filter"] = metadata["cardfilter"]
        for key in ("iscombatdamage", "combatdamageonly",
                    "noncombatdamageonly", "replaceexistingvalue"):
            if key in metadata:
                rule[key] = bool(metadata[key])
        if prop == "damagemultiplier":
            rule["value"] = max(0, int(self.modifier_value(
                param, metadata, "damagemultiplier") or 0))
        rules.append(rule)
        self._save_card_buffs(int(target), buffs, column)
        self._push_modifier_card(int(target))
        return f"rule {prop} on {hex(int(target))}"

    def discard(self, target: int | None = None) -> str:
        """Move a hand/choosing card to discard and publish its projections."""
        import game_engine

        target = self.resolved_target() if target is None else target
        if target is None:
            # A hand-discard effect is an asynchronous target request, not a
            # successful no-op.  The old BOM walker recognized this before
            # dispatching the leaf; native resolution must create the same
            # typed continuation so the parent AbilityInstance can resume at
            # the following effect after the client picks a card.
            if self.native_context:
                prompt = getattr(self.handler, "_push_discard_prompt", None)
                if callable(prompt):
                    self.bstate["rules_port_resume_effect_order"] = int(
                        self.bstate.get("resolving_effect_order", 0) or 0) + 1
                    pending = self.continuation(
                            resume_effect_order=self.bstate[
                                "rules_port_resume_effect_order"])
                    pending["instance_id"] = int(
                        getattr(self.ability, "instance_id", 1) or 1)
                    self.bstate["pending_discard_continuation"] = pending
                    result = prompt(
                        self.game, self.session, self.player_uid,
                        self.ai_uid, self.bstate,
                        ability_guid=self.bstate.get("resolving_ability", ""))
                    # AI discard is completed synchronously by the host
                    # projection; a human discard leaves the port paused.
                    if "ai_discarded_uid" not in self.bstate:
                        self.bstate["resolution_paused"] = True
                    return result or "discard: awaiting target"
            return "discard: no target"
        from pvp_db import db_card_zone_details, db_card_state_value
        details = db_card_zone_details(
            self.session.session_id, int(target), conn=self.db)
        row = ((details[0], details[1], details[2], details[3],
                db_card_state_value(self.session.session_id, int(target), conn=self.db))
               if details else None)
        if not row:
            return f"discard: target {hex(int(target))} not found"
        if str(row[3]).lower() not in ("hand", "choosing"):
            return f"discard: target {hex(int(target))} is in {row[3]}"

        from db import db_discard_card
        from rules_port.runtime_helpers import owner_uid

        owner_id = db_discard_card(
            self.session.session_id, int(target), connection=self.db,
            extra_set="card_state=0, card_damage=0, temporary_buffs=?, "
                       "temporary_attributes=0",
            extra_params=["{}"])
        if owner_id is None:
            return f"discard: target {hex(int(target))} disappeared"
        scid = game_engine.SessionCardId(game_engine.UID(int(target)))
        owner = owner_uid(owner_id, self.player_uid, self.ai_uid, self.bstate)
        tpl_guid, ct, _name, cost, atk, defense, gem = \
            self.handler._card_full_data(
                self.game, scid, row[0], row[1])
        self.game.push_card_discarded(scid, owner)
        self.game.push_card_moved(
            scid, owner, game_engine.ECardCollections.Discard,
            game_engine.ECardLocations.Top, 0)
        self.game.push_card_updated(
            scid, owner, game_engine.ECardCollections.Discard, ct,
            template_id=tpl_guid, attack=atk, defense=defense, cost=cost,
            gems=gem, state=0, nulling=(row[3] == "deck"))
        self._emit_trigger(
            "CardDiscardedEvent", int(target), owner_id,
            event_source_collection=row[3],
            event_destination_collection="discard",
            event_previous_state=int(row[4] or 0))
        return f"discarded {hex(int(target))}"

    def discard_or_sacrifice(self) -> str:
        """Apply the destination-dependent discard/sacrifice operation."""
        target = self.resolved_target()
        if target is None:
            return "discard or sacrifice: no target"
        from pvp_db import db_card_location
        location = db_card_location(
            self.session.session_id, int(target), conn=self.db)
        if location is None:
            return "discard or sacrifice: target not found"
        if location == "warzone":
            return self.sacrifice(target)
        if location == "hand":
            return self.discard(target)
        return f"discard or sacrifice: ignored {location}"

    def tunnel(self) -> str:
        """Move the resolved card underground and publish its representation.

        Tunnel is a real zone change in the original client, not a discard
        shortcut. Publish the normal exit/enter trigger pair as well as the
        destination card projection so Underground triggers remain metadata-
        driven.
        """
        # TunnelCard in the client always operates on the card resolved by
        # the active ability instance. Self-tunnel abilities (the common hand
        # activation) bind that target to ``resolving_source_uid``. The
        # legacy spell/modifier aliases can outlive a nested trigger, so they
        # must not redirect a tunnel into another player's card.
        source_uid = self.bstate.get("resolving_source_uid")
        target = self.target(default=source_uid)
        if target is None:
            return "tunnel: no target"
        from pvp_db import db_card_owner_zone_state, db_move_card_to_location
        row = db_card_owner_zone_state(
            self.session.session_id, int(target), conn=self.db)
        if not row:
            return "tunnel: target not found"
        source_owner = int(row[0] or 0)
        source_location = str(row[1] or "")
        db_move_card_to_location(
            self.session.session_id, int(target), "underground", conn=self.db)
        self.db.commit()
        from rules_port.zone_effects import project_card
        project_card(self, int(target), "underground")
        self._emit_trigger(
            "CardExitedZoneEvent", int(target), source_owner,
            event_source_collection=source_location,
            event_destination_collection="underground")
        self._emit_trigger(
            "CardEnteredZoneEvent", int(target), source_owner,
            event_source_collection=source_location,
            event_destination_collection="underground",
            event_previous_state=0)
        return f"tunneled {hex(int(target))}"

    def create_matching_token(self) -> str:
        """Create copies of the resolved target using typed effect fields."""
        target = self.resolved_target()
        if target is None:
            return "matching token: no target"
        count = self.value("m_InputValue", default=1)
        collection = self.template_value("m_CardCollection", "Warzone")
        if self.native_context or self.bstate.get("_rules_port_native_effect"):
            from rules_port.token_effects import create_matching_target
            made = create_matching_target(self, int(target), max(1, int(count)), collection)
        else:
            from abilities.framework.effects.utility import _create_matching_target
            made = _create_matching_target(
                self.game, self.session, self.db, self.handler,
                self.player_uid, self.ai_uid, self.bstate, int(target),
                max(1, int(count)), collection)
        return f"created {made} matching token(s)"

    def transform_replica(self) -> str:
        """Transform a card into a replica while retaining its zone."""
        target = self.resolved_target()
        if target is None:
            return "transform replica: no target"
        from pvp_db import db_card_zone_details
        details = db_card_zone_details(
            self.session.session_id, int(target), conn=self.db)
        if not details:
            return "transform replica: target not found"
        from rules_port.transform_effects import transform_instance
        transform_instance(self, int(target), details[0], keep_zone=True)
        return f"replicated {hex(int(target))}"

    def transform_self(self) -> str:
        """Transform the resolving source using its authored templates."""
        import random

        source = self.bstate.get("resolving_source_uid")
        if source is None:
            return "transform self: no source"
        from rules_port.transform_effects import linked_template_guids, transform_instance

        linked = linked_template_guids(
            self.db, self.bstate.get("resolving_ability"))
        if not linked:
            return "transform self: no template"
        new_template = random.choice(linked)
        transform_instance(self, int(source), new_template)
        return f"transformed self -> {new_template[:8]}"

    def transform_card_random(self) -> str:
        """Run the authored random-filter transform through RulesPort."""
        if self.native_context or self.bstate.get("_rules_port_native_effect"):
            from rules_port.transform_effects import transform_card_at_random
            return transform_card_at_random(self)
        from abilities.framework.bom import _transform_card_at_random_legacy
        return _transform_card_at_random_legacy(
            self.game, self.session, self.db, self.handler,
            self.player_uid, self.ai_uid, self.bstate,
            self.effect_guid, self.param)

    def transform_card(self) -> str:
        """Run the authored direct-template transform operation."""
        if self.native_context or self.bstate.get("_rules_port_native_effect"):
            if self.bstate.get("_skip_transform"):
                return "transform skipped (gate not met)"
            from rules_port.transform_effects import transform_instance
            template = self.template_value("m_CardTemplateId", "")
            if not template or str(template).lower() == "0" * 36:
                raise RuntimeError(
                    "RulesPort TransformCard effect is missing typed target template")
            if not template:
                return "transform: no template"
            pending = list(self.bstate.pop("pending_transform_cards", []) or [])
            if pending:
                for value in pending:
                    target = value[0] if isinstance(value, (tuple, list)) else value
                    transform_instance(self, int(target), str(template),
                                       keep_zone=True)
                return f"transform {len(pending)} -> {str(template)[:8]}"
            target = (self.bstate.get("player_transform_target") or
                      self.bstate.get("player_mod_target") or
                      self.bstate.get("player_shift_source") or
                      self.bstate.get("resolving_source_uid"))
            if target is None:
                return "transform: no target"
            transform_instance(self, int(target), str(template))
            return f"transformed {hex(int(target))} -> {str(template)[:8]}"
        from abilities.framework.bom import _transform_card_legacy

        return _transform_card_legacy(
            self.game, self.session, self.db, self.handler,
            self.player_uid, self.ai_uid, self.bstate,
            self.effect_guid, self.param)

    def create_token_copy(self) -> str:
        """Run the authored token-copy operation through RulesPort."""
        if self.native_context or self.bstate.get("_rules_port_native_effect"):
            from rules_port.token_effects import create_token_copy
            return create_token_copy(self)
        from abilities.framework.bom import _create_token_copy_legacy

        return _create_token_copy_legacy(
            self.game, self.session, self.db, self.handler,
            self.player_uid, self.ai_uid, self.bstate,
            self.effect_guid, self.param)

    def destroy_by_defense(self) -> str:
        """Destroy warzone troops that fail the authored defense survival roll."""
        if self.native_context or self.bstate.get("_rules_port_native_effect"):
            from rules_port.destruction_effects import destroy_by_defense
            return destroy_by_defense(self)
        import random

        from abilities.framework.kill_troop import kill_troop

        from pvp_db import db_warzone_troop_uids, db_card_effective_defense
        rows = db_warzone_troop_uids(self.session.session_id, conn=self.db)
        destroyed = 0
        for (uid,) in rows:
            stat_row = db_card_effective_defense(
                self.session.session_id, int(uid), conn=self.db)
            defense = ((stat_row[0] or 0) + (stat_row[1] or 0)
                       if stat_row else 0)
            if random.random() > 0.10 * defense:
                kill_troop(
                    self.game, self.session, self.db, self.handler,
                    self.player_uid, self.ai_uid, int(uid), self.bstate,
                    cause="effect")
                destroyed += 1
        return f"destroyed {destroyed}/{len(rows)}"

    def modifier_value(self, param: dict | None, metadata: dict | None,
                       property_name: str) -> int:
        """Resolve a typed CardModifier operand from Records and live state."""
        payload = dict(param or {})
        metadata = metadata or {}
        if metadata.get("input_variable"):
            payload.setdefault("input_variable", metadata["input_variable"])
        if metadata.get("input_value") and not payload.get("amount"):
            payload["amount"] = metadata["input_value"]
        if self.native_context:
            # Native effects must resolve operands through the RulesPort
            # variable evaluator.  Calling the historical leaf evaluator here
            # would make the effect state hybrid even when its outer resolver
            # is native.
            from pvp_db import db_ability_raw_json
            from rules_port.static_rules import _native_leaf_value
            raw = db_ability_raw_json(self.ability_guid, conn=self.db) or "{}"
            value = _native_leaf_value(
                self.db, self.session.session_id, self.bstate,
                int(self.bstate.get("resolving_source_uid") or 0),
                int(self.bstate.get("resolving_owner_id", 0) or 0),
                payload, raw)
            if value is None:
                return 0
            return int(value[1] or 0)
        from abilities.framework.fields import ability_record
        from abilities.framework.statics import _leaf_numeric_value
        raw = json.dumps(ability_record(self.db, self.ability_guid))
        return int(_leaf_numeric_value(
            self.db, self.session.session_id, self.bstate, payload, raw,
            self.bstate.get("resolving_owner_id", 0),
            int(self.bstate.get("resolving_source_uid") or 0),
            property_name) or 0)

    def damage_modifier(self, param: dict | None,
                        metadata: dict | None = None) -> str:
        """Resolve and apply a metadata-defined damage modifier.

        Escalation and X-cost damage are still represented by the same typed
        modifier but have activation-wide state transitions; keeping those
        branches here makes the leaf a one-line adapter without pretending
        they are ordinary static damage.
        """
        import re

        param = dict(param or {})
        text = str(param.get("text") or "")
        target = self.modifier_target()
        if target is None:
            if self.native_context:
                from rules_port.targeting import implicit_champion_target
                target = implicit_champion_target(
                    self.db, self.session, self.handler, self.bstate)
            else:
                from abilities.framework.bom import _champion_target_uid
                target = _champion_target_uid(
                    self.handler, self.bstate, self.db, self.session)
        if target is None:
            if self.native_context:
                from rules_port.targeting import implicit_champion_target
                target = implicit_champion_target(
                    self.db, self.session, self.handler, self.bstate,
                    opposing=True)
            else:
                from abilities.framework.bom import _opposing_champion_uid
                target = _opposing_champion_uid(
                    self.handler, self.bstate, self.db, self.session)
        if target is None:
            return "damage: no target"

        lower = text.lower()
        esc = re.search(r"esc:(\d+)", lower)
        if esc:
            base = int(esc.group(1))
            uses_key = ("ai_escalation_uses"
                        if self.bstate.get("resolving_owner_id") == 0
                        else "player_escalation_uses")
            uses = int(self.bstate.get(uses_key, 0) or 0)
            amount = base * (uses + 1)
            if not self.bstate.get("_esc_counted_this_resolution"):
                self.bstate[uses_key] = uses + 1
                self.bstate["_esc_counted_this_resolution"] = True
        elif "x damage" in lower:
            amount = int(self.bstate.get("x_cost", 0) or 0)
        else:
            amount = self.modifier_value(param, metadata, "damage")
        if amount <= 0:
            match = re.search(r"deal\s+(\d+)\s+damage", lower)
            if match:
                amount = int(match.group(1))
        if amount > 0 and not esc and "esc " in lower:
            self.bstate["player_escalation_uses"] = int(
                self.bstate.get("player_escalation_uses", 0) or 0) + 1
        if amount <= 0:
            return "damage: amount 0"
        return self.damage(int(target), amount)

    def stat_modifier(self, param: dict | None,
                      metadata: dict | None = None) -> str:
        """Resolve and apply a typed attack or defense modifier."""
        import re

        param = dict(param or {})
        metadata = metadata or {}
        property_name = str(
            metadata.get("property") or param.get("property") or "")
        target = self.modifier_target()
        amount = self.modifier_value(param, metadata, property_name)
        attack = amount if property_name == "attack" else 0
        defense = amount if property_name == "defense" else 0
        text = str(param.get("text") or "").lower()

        # These are typed stat effects whose operand is derived from a prior
        # card, not a static EffectInputVariable. Keep the derivation here so
        # every stat leaf still shares the same persistence/event operation.
        if attack == 0 and defense == 0 and \
                "equal to this troop's [def]" in text:
            source_uid = self.bstate.get("resolving_source_uid")
            if source_uid is not None:
                from pvp_db import db_card_effective_defense
                row = db_card_effective_defense(
                    self.session.session_id, int(source_uid), conn=self.db)
                if row:
                    value = int(row[0] or 0) + int(row[1] or 0)
                    if property_name == "attack":
                        attack = value
                    else:
                        defense = value
        if attack == 0 and defense == 0 and "voided troop's" in text:
            stats = self.bstate.get("champion_voided_stats") or {}
            match = re.search(r"plus\s+(\d+)", text)
            value = int(stats.get(
                "atk" if property_name == "attack" else "def", 0) or 0)
            value += int(match.group(1)) if match else 0
            if property_name == "attack":
                attack = value
            else:
                defense = value
            created = self.bstate.get("created_token_uids") or []
            if created:
                target = int(created[0])
                self.bstate["player_mod_target"] = target

        if target is None:
            return f"stat mod: {property_name} {amount:+}"
        this_turn = param.get("duration") in (
            "EndOfTurn", "BeginningOfOwnersTurn",
            "AfterCardsReadyOnPlayersTurn")
        self.stat_mod(target, attack, defense, this_turn=this_turn)
        return f"mod {hex(int(target))} {attack:+}/{defense:+}"



    def stat_mod(self, target: int, attack: int = 0, defense: int = 0,
                 this_turn: bool = False):
        """Apply a card stat modifier through the shared event path."""
        if self.native_context or self.bstate.get("_rules_port_native_effect"):
            from rules_port.stat_effects import apply_stat_mod
            return apply_stat_mod(
                self, int(target), int(attack), int(defense),
                this_turn=this_turn)
        from abilities.framework.stat_mod import apply_card_stat_mod

        return apply_card_stat_mod(
            self.game, self.session, self.db, self.handler,
            self.player_uid, self.ai_uid, int(target), int(attack),
            int(defense), this_turn=this_turn, bstate=self.bstate)

    def card_modifier(self) -> str:
        """Run the metadata CardModifier operation behind the context ABI.

        CardModifier is a family of typed operations (health, resources,
        attributes, counters, cost changes, and stat/damage modifiers), not a
        single primitive. Keep its compatibility-heavy orchestration in one
        implementation while exposing only the context-shaped leaf boundary.
        """
        if self.native_context or self.bstate.get("_rules_port_native_effect"):
            # Keep direct context callers on the same typed implementation as
            # the RulesPort effect dispatcher.  The old BOM helper is only a
            # compatibility path for legacy callers.
            from rules_port.effects import _card_modifier
            return _card_modifier(self, {"param": self.param})

        from abilities.framework.bom import _card_modifier_legacy

        return _card_modifier_legacy(
            self.game, self.session, self.db, self.handler,
            self.player_uid, self.ai_uid, self.bstate,
            self.effect_guid, self.param)

    def move_card_to_zone_legacy(self) -> str:
        """Run the metadata zone transition and its client-event contract."""
        from abilities.framework.bom import _move_card_to_zone_legacy

        return _move_card_to_zone_legacy(
            self.game, self.session, self.db, self.handler,
            self.player_uid, self.ai_uid, self.bstate,
            self.effect_guid, self.param)

    def return_to_hand(self) -> str:
        """Return the resolved card through the typed hand transition."""
        if not (self.native_context or self.bstate.get("_rules_port_native_effect")):
            from abilities.framework.bom import _move_card_to_zone_legacy
            return _move_card_to_zone_legacy(
                self.game, self.session, self.db, self.handler,
                self.player_uid, self.ai_uid, self.bstate,
                self.effect_guid, '{"destination":"hand"}')
        target = self.resolved_target()
        if target is None:
            target = self.bstate.get("resolving_source_uid")
        return self.move_card_to_hand(target)

    def move_card_to_hand(self, target: int | None = None) -> str:
        """Move one card to hand while retaining its current controller."""
        if target is None:
            return "return to hand: no target"
        target = int(target)
        from pvp_db import (db_card_owner_zone_state,
                            db_card_zone_details, db_move_card_for_effect,
                            db_card_state_value)
        old = db_card_owner_zone_state(
            self.session.session_id, target, conn=self.db)
        if not old:
            return "return to hand: target not found"
        old_location = str(old[1] or "").lower()
        old_state = int(old[2] or 0)
        db_move_card_for_effect(
            self.session.session_id, target, "hand", 100,
            0, conn=self.db)
        self.db.commit()
        details = db_card_zone_details(
            self.session.session_id, target, conn=self.db)
        if not details:
            return "return to hand: target disappeared"
        from rules_port.runtime_helpers import owner_uid, card_collection_for_location
        import game_engine
        scid = game_engine.SessionCardId(game_engine.UID(target))
        tpl, ct, _name, cost, attack, defense, gem = self.handler._card_full_data(
            self.game, scid, details[0])
        owner = owner_uid(details[2], self.player_uid, self.ai_uid, self.bstate)
        self.game.push_card_moved(
            scid, owner, game_engine.ECardCollections.Hand,
            game_engine.ECardLocations.Top, 1)
        self.game.push_card_updated(
            scid, owner, game_engine.ECardCollections.Hand, ct,
            template_id=tpl, cost=cost, attack=attack, defense=defense,
            gems=gem, state=int(db_card_state_value(
                self.session.session_id, target, conn=self.db) or 0))
        self._emit_trigger(
            "CardEnteredZoneEvent", target, int(details[2] or 0),
            event_source_collection=old_location,
            event_destination_collection="hand", event_previous_state=old_state)
        return f"returned {hex(target)} to hand"

    def clear_stored(self) -> str:
        """Clear targets/lists remembered by the resolving source ability."""
        ability = str(self.ability_guid or "")
        self.bstate.setdefault("stored_targets", {}).pop(ability, None)
        self.bstate.setdefault("ability_lists", {}).pop(ability, None)
        return "cleared stored targets"

    def set_responsible_player(self) -> str:
        """Mirror SetResponsiblePlayer by adopting the target's controller."""
        target = self.resolved_target()
        owner = self.target_owner(target, default=None)
        if target is None or owner is None:
            return "set responsible player: no target"
        self.bstate["resolving_owner_id"] = int(owner)
        self.bstate["resolving_responsible_player_id"] = int(owner)
        return f"responsible player {int(owner)}"

    def copy_ability_variable(self) -> str:
        """Copy one typed EffectField into the destination variable."""
        from rules_port.fields import resolve_field
        source = self.template_value("m_Source", None)
        destination = self.template_value("m_Destination", None)
        def variable_name(value):
            if hasattr(value, "field"):
                for key in ("m_InputVariableName", "m_VariableName",
                            "variable", "name"):
                    found = value.field(key, None)
                    if found:
                        return str(found)
            if isinstance(value, dict):
                for key in ("m_InputVariableName", "m_VariableName",
                            "variable", "name"):
                    if value.get(key):
                        return str(value[key])
            return str(value or "")
        dest_name = variable_name(destination)
        variables = self.bstate.setdefault("ability_variables", {})
        value = resolve_field(source, variables,
                              self.bstate.get("effect_outputs") or {},
                              self.bstate, 0)
        if not dest_name:
            return "copy ability variable: missing destination"
        variables[dest_name] = int(value or 0)
        return f"copied {dest_name}={variables[dest_name]}"

    def set_card_count_variable(self) -> str:
        """Count typed cards across the requested player collections."""
        import json
        from rules_port.targeting import evaluate_card_filter
        from rules_port.fields import effect_template

        template = effect_template(self.ability, self.effect_guid) or {}
        name = template.get("m_VariableName") or ""
        if not name and self.param:
            try:
                name = (json.loads(self.param) or {}).get("variable", "")
            except (TypeError, ValueError, json.JSONDecodeError):
                name = ""
        if not name:
            return "card count: missing variable"
        collection = template.get("m_CollectionFlags", "Deck|Hand|Warzone|Discard|Void")
        if isinstance(collection, (list, tuple, set)):
            collections = {str(item).rsplit(".", 1)[-1].lower()
                           for item in collection}
        else:
            collections = {part.strip().rsplit(".", 1)[-1].lower()
                           for part in str(collection).replace(",", "|").split("|")
                           if part.strip()}
        locations = {"deck": "deck", "hand": "hand", "warzone": "warzone",
                     "discard": "discard", "void": "void",
                     "playedresources": "playedresources"}
        locations = [locations[item] for item in collections if item in locations]
        if not locations:
            self.bstate.setdefault("ability_variables", {})[str(name)] = 0
            return f"counted {name}=0"
        player_filter = str(template.get("m_PlayerFilter", "Self")).rsplit(".", 1)[-1].lower()
        owner = int(self.bstate.get("resolving_owner_id", 0) or 0)
        if player_filter in ("multipleopponents", "singleopponent"):
            owners = [int(pid) for pid in (self.bstate.get("player_ids") or [])
                      if int(pid) != owner]
            if not owners:
                owners = [int(self.bstate.get("opponent_id", 0) or 0)]
        elif player_filter in ("multipleplayers", "allplayers"):
            owners = [int(pid) for pid in (self.bstate.get("player_ids") or [])]
        else:
            owners = [owner]
        card_filter = template.get("m_CardFilter") or {}
        if hasattr(card_filter, "to_dict"):
            card_filter = card_filter.to_dict()
        from pvp_db import db_cards_for_filter
        rows = db_cards_for_filter(
            self.session.session_id, owners, locations, conn=self.db)
        count = 0
        for row in rows:
            card = {"card_uid": int(row[0]), "card_type": row[1] or "",
                    "location": row[2] or "", "user_id": int(row[3] or 0),
                    "state": int(row[4] or 0), "attack": int(row[5] or 0),
                    "defense": int(row[6] or 0), "name": row[7] or "",
                    "cost": int(row[8] or 0), "subtype": row[9] or ""}
            if evaluate_card_filter(card, card_filter, self.bstate.get(
                    "resolving_source_uid"), ability_state=self.bstate, db=self.db):
                count += 1
        self.bstate.setdefault("ability_variables", {})[str(name)] = count
        return f"counted {name}={count}"

    def set_card_integer_variable(self) -> str:
        """Apply the client Add/Remove/Set operation to source card data."""
        name = str(self.template_value("m_VariableName", "") or "")
        if not name and self.param:
            try:
                name = str((json.loads(self.param) or {}).get("variable", ""))
            except (TypeError, ValueError, json.JSONDecodeError):
                name = ""
        source = self.bstate.get("resolving_source_uid")
        if not name or source is None:
            return "card integer variable: missing name/source"
        operation = str(self.template_value("m_Operation", "Set")).rsplit(".", 1)[-1].lower()
        value = int(self.value("m_InputValue", 0) or 0)
        buffs = self._card_buffs(int(source), "permanent_buffs")
        values = buffs.setdefault("card_integer_variables", {})
        old = int(values.get(name, 0) or 0)
        new = old + value if operation == "add" else old - value if operation == "remove" else value
        values[name] = new
        self._save_card_buffs(int(source), buffs, "permanent_buffs")
        self.bstate.setdefault("card_integer_variables", {})[name] = new
        self.bstate.setdefault("ability_variables", {})[name] = new
        return f"card variable {name}={new}"

    def set_constant_value_variable(self) -> str:
        """Set an ability variable from the deprecated typed m_Value field."""
        name = str(self.template_value("m_VariableName", "") or "")
        value = self.template_value("m_Value", None)
        if self.param:
            try:
                payload = json.loads(self.param)
                name = name or str(payload.get("variable", ""))
                if value is None:
                    value = payload.get("value", 0)
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        if not name:
            return "constant variable: missing name"
        value = int(value or 0)
        self.bstate.setdefault("ability_variables", {})[name] = value
        return f"constant variable {name}={value}"

    def transform_card_to_target(self) -> str:
        """Transform the source card into a copy of the resolved target."""
        source = self.bstate.get("resolving_source_uid")
        target = self.resolved_target()
        if source is None or target is None:
            return "transform to target: missing source/target"
        from pvp_db import db_card_zone_details
        details = db_card_zone_details(
            self.session.session_id, int(target), conn=self.db)
        if not details:
            return "transform to target: target not found"
        from rules_port.transform_effects import transform_instance
        transform_instance(self, int(source), details[0])
        return f"transformed {hex(int(source))} to target copy"

    def finish_moving_to_warzone(self) -> str:
        """Complete a deferred move into Warzone through the typed path."""
        if not (self.native_context or self.bstate.get("_rules_port_native_effect")):
            from abilities.framework.bom import _move_card_to_zone_legacy
            return _move_card_to_zone_legacy(
                self.game, self.session, self.db, self.handler,
                self.player_uid, self.ai_uid, self.bstate,
                self.effect_guid, '{"destination":"warzone"}')
        target = self.resolved_target() or self.bstate.get(
            "resolving_source_uid")
        return self._move_simple_zone(target, "warzone")

    def finish_resolving_card(self) -> str:
        """Complete a spell's resolution by moving it to Discard."""
        if not (self.native_context or self.bstate.get("_rules_port_native_effect")):
            target = self.resolved_target()
            if target is None:
                target = self.bstate.get("resolving_source_uid")
            if target is None:
                return "finish resolving: no card"
            from abilities.framework.bom import _move_card_to_zone_legacy
            return _move_card_to_zone_legacy(
                self.game, self.session, self.db, self.handler,
                self.player_uid, self.ai_uid, self.bstate,
                self.effect_guid, '{"destination":"discard"}')
        target = self.resolved_target()
        if target is None:
            target = self.bstate.get("resolving_source_uid")
        return self._move_simple_zone(target, "discard",
                                      label="finish resolving")

    def _move_simple_zone(self, target: int | None, destination: str,
                          *, label="move") -> str:
        """Apply one ordinary zone transition and its typed event contract."""
        if target is None:
            return f"{label}: no card"
        target = int(target)
        destination = str(destination).lower()
        import game_engine
        from pvp_db import (db_card_owner_zone_state, db_card_zone_details,
                            db_card_state_value, db_move_card_for_effect)
        old = db_card_owner_zone_state(
            self.session.session_id, target, conn=self.db)
        if not old:
            return f"{label}: card not found"
        old_location, old_state = str(old[1] or "").lower(), int(old[2] or 0)
        position = 100 if destination == "hand" else 0
        clear_dead = destination == "warzone"
        clear_bits = game_engine.ECardStates.Dead if clear_dead else 0
        db_move_card_for_effect(
            self.session.session_id, target, destination, position,
            0 if destination != "warzone" else old_state,
            clear_dead=clear_dead, clear_bits=clear_bits, conn=self.db)
        self.db.commit()
        details = db_card_zone_details(
            self.session.session_id, target, conn=self.db)
        if not details:
            return f"{label}: card disappeared"
        from rules_port.runtime_helpers import owner_uid, card_collection_for_location
        scid = game_engine.SessionCardId(game_engine.UID(target))
        tpl, ct, _name, cost, attack, defense, gem = self.handler._card_full_data(
            self.game, scid, details[0])
        owner = owner_uid(details[2], self.player_uid, self.ai_uid, self.bstate)
        collection = card_collection_for_location(destination)
        if destination == "discard":
            self.game.push_card_discarded(scid, owner)
        self.game.push_card_moved(
            scid, owner, collection, game_engine.ECardLocations.Top, 0)
        self.game.push_card_updated(
            scid, owner, collection, ct, template_id=tpl, cost=cost,
            attack=attack, defense=defense, gems=gem,
            state=int(db_card_state_value(
                self.session.session_id, target, conn=self.db) or 0))
        event = "CardEnteredZoneEvent"
        self._emit_trigger(
            event, target, int(details[2] or 0),
            event_source_collection=old_location,
            event_destination_collection=destination,
            event_previous_state=old_state)
        if destination == "discard":
            self._emit_trigger(
                "CardDiscardedEvent", target, int(details[2] or 0),
                event_source_collection=old_location,
                event_destination_collection=destination,
                event_previous_state=old_state)
        return f"{label}: moved {hex(target)} to {destination}"

    def move_card_to_zone(self) -> str | None:
        """Handle ordinary typed MoveCardToZone destinations.

        Deck reinsertion, choice-zone cleanup, and authored multi-card moves
        are rejected as unsupported until their ordering contracts are ported.
        The native RulesPort resolver turns a ``None`` result into an explicit
        error; it never falls through to the legacy leaf dispatcher.
        """
        if not (self.native_context or self.bstate.get("_rules_port_native_effect")):
            return self.move_card_to_zone_legacy()
        destination = self.template_value("m_DestinationCollection", "")
        if not destination and self.param:
            try:
                destination = json.loads(self.param).get("destination", "")
            except (TypeError, ValueError, json.JSONDecodeError):
                destination = ""
        destination = str(destination or "").rsplit(".", 1)[-1].lower()
        if destination == "playedresources":
            # Choice effects use a typed bulk transition: temporary cards in
            # the Choosing collection are consumed into PlayedResources
            # before the next choice prompt is created.  Keep this operation
            # native and metadata-driven; it must not enter the legacy BOM.
            from rules_port.choice_effects import _clear_choice_zone
            _clear_choice_zone(self)
            return "cleared choice zone"
        if destination not in {"hand", "discard", "void", "warzone",
                               "underground"}:
            return None
        target = self.resolved_target()
        if target is None and destination == "underground":
            target = self.bstate.get("resolving_source_uid")
        return self._move_simple_zone(target, destination)

    def target_player_takes_control(self) -> str:
        """Transfer the resolving source to the target player's control."""
        source = self.bstate.get("resolving_source_uid")
        target = self.resolved_target()
        owner = self.target_owner(target, default=None)
        if source is None or owner is None:
            return "take control: missing source/target owner"
        from pvp_db import db_card_owner_zone_state, db_set_card_owner
        row = db_card_owner_zone_state(
            self.session.session_id, int(source), conn=self.db)
        if not row:
            return "take control: source not found"
        columns = {row[1] for row in self.db.execute(
            "PRAGMA table_info(game_cards)").fetchall()}
        if "owner_user_id" in columns:
            self.db.execute(
                "UPDATE game_cards SET user_id=?, owner_user_id=? "
                "WHERE session_id=? AND card_uid=?",
                (int(owner), int(owner), self.session.session_id, int(source)))
        else:
            db_set_card_owner(
                self.session.session_id, int(source), owner, conn=self.db)
        self.db.commit()
        from rules_port.zone_effects import project_card
        project_card(self, int(source), str(row[1]))
        return f"source control -> {int(owner)}"

    def steal_card(self) -> str:
        """Steal the top card of the target player's deck into responsibility hand."""
        target = self.resolved_target()
        target_owner = self.target_owner(target, default=None)
        thief = int(self.bstate.get("resolving_responsible_player_id",
                                    self.bstate.get("resolving_owner_id", 0)) or 0)
        if target_owner is None:
            return "steal: missing target owner"
        from pvp_db import db_deck_top_card_details, db_steal_card_to_hand
        top = db_deck_top_card_details(
            self.session.session_id, int(target_owner), conn=self.db)
        if not top:
            return "steal: target deck empty"
        card_uid = int(top[1])
        columns = {item[1] for item in self.db.execute(
            "PRAGMA table_info(game_cards)").fetchall()}
        if "owner_user_id" in columns:
            self.db.execute(
                "UPDATE game_cards SET user_id=?, owner_user_id=?, location='hand', "
                "position=100, card_state=0 WHERE session_id=? AND card_uid=?",
                (thief, thief, self.session.session_id, card_uid))
        else:
            db_steal_card_to_hand(
                self.session.session_id, card_uid, thief, conn=self.db)
        self.db.commit()
        from rules_port.zone_effects import project_card
        project_card(self, card_uid, "hand")
        return f"stole {hex(card_uid)}"

    def steal_effects(self) -> str:
        """Move runtime effect records from the target card to the source."""
        source = self.bstate.get("resolving_source_uid")
        target = self.resolved_target()
        if source is None or target is None:
            return "steal effects: missing source/target"
        moved = 0
        for column in ("permanent_buffs", "temporary_buffs"):
            source_buffs = self._card_buffs(int(source), column)
            target_buffs = self._card_buffs(int(target), column)
            for key in ("stat_modifiers", "rule_modifiers", "attribute_modifiers",
                        "cost_modifiers", "damage_shields"):
                values = target_buffs.pop(key, [])
                if not isinstance(values, list) or not values:
                    continue
                source_buffs.setdefault(key, []).extend(values)
                moved += len(values)
            self._save_card_buffs(int(target), target_buffs, column)
            self._save_card_buffs(int(source), source_buffs, column)
        self._push_modifier_card(int(source))
        self._push_modifier_card(int(target))
        return f"stole {moved} effect(s)"

    def lose_game(self) -> str:
        """Eliminate the responsible player through the session boundary."""
        player = int(self.bstate.get("resolving_responsible_player_id",
                                    self.bstate.get("resolving_owner_id", 0)) or 0)
        for method_name in ("eliminate_player", "EliminatePlayer"):
            method = getattr(self.session, method_name, None)
            if callable(method):
                method(player, "ScriptedEvent")
                return f"eliminated player {player}"
        self.bstate.setdefault("eliminated_players", []).append(player)
        self.bstate["eliminated_players"] = list(dict.fromkeys(
            self.bstate["eliminated_players"]))
        return f"eliminated player {player}"

    def revert_transformed_card(self) -> str:
        """Restore a transformed card's original template in its current zone."""
        target = self.resolved_target()
        if target is None:
            return "revert transform: no target"
        from pvp_db import db_card_original_and_template
        row = db_card_original_and_template(
            self.session.session_id, int(target), conn=self.db)
        original = row[0] if row and row[0] else None
        if not original or str(original).lower() == str(row[1]).lower():
            return "revert transform: no original template"
        if self.native_context or self.bstate.get("_rules_port_native_effect"):
            from rules_port.transform_effects import transform_instance
            transform_instance(self, int(target), original, keep_zone=True)
            return f"reverted {hex(int(target))}"
        from abilities.framework.transform import transform_card
        transform_card(self.handler, self.game, self.session,
                       self.player_uid, self.ai_uid, int(target), original,
                       keep_zone=True, bstate=self.bstate)
        return f"reverted {hex(int(target))}"

    def player_attribute(self) -> str:
        """Apply typed EPlayerAttributes bits to the target controller."""
        from rules_port.attribute_effects import attribute_bits_from_flags
        flags = self.template_value("m_AttributeFlags", 0)
        bits = attribute_bits_from_flags(flags)
        if not bits:
            return "player attribute: none"
        target = self.resolved_target()
        owner = self.target_owner(target,
                                  default=self.bstate.get("resolving_owner_id", 0))
        if owner is None:
            return "player attribute: no owner"
        key = str(int(owner))
        values = self.bstate.setdefault("player_attribute_flags", {})
        current = int(values.get(key, 0) or 0)
        operation = str(self.template_value("m_Operation", "Add")).rsplit(".", 1)[-1].lower()
        values[key] = (current | bits) if operation == "add" else (current & ~bits)
        return f"player attributes {key}={values[key]}"

    def exchange_cards(self) -> str:
        """Exchange the typed number of deck cards between two controllers."""
        target_owner = self.target_owner(self.resolved_target(), default=None)
        player_one = int(self.bstate.get("resolving_responsible_player_id",
                                         self.bstate.get("resolving_owner_id", 0)) or 0)
        if target_owner is None:
            return "exchange: no target owner"
        amount = max(0, int(self.value("m_Amount", 0) or 0))
        if amount <= 0:
            return "exchange: zero cards"
        from pvp_db import db_deck_card_uids, db_set_card_owners
        def top(owner):
            return [int(row[0]) for row in db_deck_card_uids(
                self.session.session_id, int(owner), amount, conn=self.db)]
        first, second = top(player_one), top(int(target_owner))
        count = min(len(first), len(second))
        db_set_card_owners([
            (self.session.session_id, left, target_owner)
            for left, right in zip(first[:count], second[:count])
        ] + [
            (self.session.session_id, right, player_one)
            for left, right in zip(first[:count], second[:count])
        ], conn=self.db)
        self.db.commit()
        return f"exchanged {count} card(s)"

    def zombie_plague(self) -> str:
        """Void a zero-defense troop and create the typed zombie token."""
        target = self.resolved_target()
        if target is None:
            return "zombie plague: no target"
        from pvp_db import db_card_type_effective_defense
        row = db_card_type_effective_defense(
            self.session.session_id, int(target), conn=self.db)
        if not row or "troop" not in str(row[0] or "").lower():
            return "zombie plague: target is not a troop"
        if int(row[1] or 0) + int(row[2] or 0) > 0:
            return "zombie plague: defense remains"
        self.void_card()
        guid = self.template_value("m_CardTemplateId", "")
        if not guid:
            return "zombie plague: voided (token metadata missing)"
        if self.native_context or self.bstate.get("_rules_port_native_effect"):
            from rules_port.token_effects import summon_token
            return summon_token(self, {"token_guid": str(guid).lower(),
                                        "amount": 1, "collection": "Warzone"})
        from abilities.framework.effects.tokens import summon_token
        return summon_token(self.game, self.session, self.db, self.handler,
                            self.player_uid, self.ai_uid, self.bstate,
                            self.effect_guid, json.dumps({"token_guid": str(guid).lower(),
                                                         "amount": 1, "collection": "Warzone"}))

    def xarlox(self) -> str:
        """Mill three typed deck cards, summoning one token per milled troop."""
        owner = self.target_owner(self.resolved_target(), default=
                                  self.bstate.get("resolving_owner_id", 0))
        if owner is None:
            return "xarlox: no owner"
        token_guid = self.template_value("m_CardTemplateId", "")
        milled = troops = 0
        if self.native_context or self.bstate.get("_rules_port_native_effect"):
            from rules_port.token_effects import summon_token
        else:
            from abilities.framework.effects.utility import _push_card_in_zone
            from abilities.framework.effects.tokens import summon_token
        from pvp_db import db_deck_top_card_type, db_move_card_to_discard_reset
        for _ in range(3):
            row = db_deck_top_card_type(
                self.session.session_id, int(owner), conn=self.db)
            if not row:
                break
            uid, card_type = int(row[0]), str(row[1] or "")
            db_move_card_to_discard_reset(
                self.session.session_id, uid, conn=self.db)
            self.db.commit()
            if self.native_context or self.bstate.get("_rules_port_native_effect"):
                from rules_port.zone_effects import project_card
                project_card(self, uid, "discard")
            else:
                _push_card_in_zone(self.game, self.session, self.db, self.handler,
                                   self.player_uid, self.ai_uid, self.bstate,
                                   uid, "discard")
            milled += 1
            if "troop" in card_type.lower():
                troops += 1
                if token_guid:
                    if self.native_context or self.bstate.get("_rules_port_native_effect"):
                        summon_token(self, {"token_guid": str(token_guid).lower(),
                                            "amount": 1, "collection": "Warzone"})
                    else:
                        summon_token(self.game, self.session, self.db, self.handler,
                                     self.player_uid, self.ai_uid, self.bstate,
                                     self.effect_guid, json.dumps({
                                         "token_guid": str(token_guid).lower(),
                                         "amount": 1, "collection": "Warzone"}))
        return f"xarlox milled {milled}, summoned {troops}"

    def plan_c(self) -> str:
        """Execute the temporary client Plan C resource-to-token effect."""
        import json
        native = self.native_context or self.bstate.get(
            "_rules_port_native_effect")
        if native:
            from rules_port.token_effects import summon_token
        else:
            from abilities.framework.effects.tokens import summon_token
        owner = self.target_owner(self.resolved_target(), default=
                                  self.bstate.get("resolving_owner_id", 0))
        if owner is None:
            return "plan c: no owner"
        side = "player" if int(owner) else "ai"
        total_key, current_key = f"{side}_total_resources", f"{side}_resources"
        total = int(self.bstate.get(total_key, 0) or 0)
        amount = total // 2
        self.bstate[total_key] = 0
        self.bstate[current_key] = 0
        setattr(self.game, total_key, 0)
        setattr(self.game, current_key, 0)
        guid = self.template_value("m_CardTemplateId", "")
        if not guid or amount <= 0:
            return f"plan c: spent {total}, created 0"
        collection = self.template_value("m_CardCollection", "Warzone")
        payload = {"token_guid": str(guid).lower(), "amount": amount,
                   "collection": str(collection).rsplit(".", 1)[-1]}
        if native:
            return summon_token(self, payload)
        return summon_token(self.game, self.session, self.db, self.handler,
                            self.player_uid, self.ai_uid, self.bstate,
                            self.effect_guid, json.dumps(payload))

    def merge_card_collections(self) -> str:
        """Merge typed source collection into destination for target owner."""
        owner = self.target_owner(self.resolved_target(), default=
                                  self.bstate.get("resolving_owner_id", 0))
        source = str(self.template_value("m_SourceCollection", "")).rsplit(".", 1)[-1].lower()
        destination = str(self.template_value("m_DestinationCollection", "")).rsplit(".", 1)[-1].lower()
        loc = {"deck": "deck", "hand": "hand", "discard": "discard",
               "void": "void", "warzone": "warzone"}
        if owner is None or source not in loc or destination not in loc:
            return "merge collections: invalid metadata"
        from pvp_db import db_ordered_zone_uids, db_move_card_to_location
        rows = db_ordered_zone_uids(
            self.session.session_id, int(owner), loc[source], conn=self.db)
        for index, row in enumerate(rows):
            db_move_card_to_location(
                self.session.session_id, int(row[0]), loc[destination],
                position=index, conn=self.db)
        self.db.commit()
        return f"merged {len(rows)} card(s) {source}->{destination}"

    def shuffle_collection(self) -> str:
        """Shuffle the typed target controller collection and renumber slots."""
        import random
        target = self.resolved_target()
        owner = self.target_owner(target, default=self.bstate.get(
            "resolving_owner_id", 0))
        collection = self.template_value("m_Collection", "Deck")
        collection = str(collection).rsplit(".", 1)[-1].lower()
        location = {"deck": "deck", "hand": "hand", "discard": "discard",
                    "void": "void", "warzone": "warzone"}.get(collection)
        if owner is None or not location:
            return "shuffle: invalid collection"
        from pvp_db import db_ordered_zone_uids, db_set_card_positions
        rows = db_ordered_zone_uids(
            self.session.session_id, int(owner), location, conn=self.db)
        uids = [int(row[0]) for row in rows]
        random.shuffle(uids)
        db_set_card_positions(
            [(self.session.session_id, uid, index)
             for index, uid in enumerate(uids)], conn=self.db)
        self.db.commit()
        return f"shuffled {location} ({len(uids)})"

    def reveal_cards(self) -> str:
        """Run the metadata reveal through RulesPort."""
        if self.native_context or self.bstate.get("_rules_port_native_effect"):
            from rules_port.reveal_effects import reveal_cards
            return reveal_cards(self)
        from abilities.framework.bom import _reveal_cards_legacy

        return _reveal_cards_legacy(
            self.game, self.session, self.db, self.handler,
            self.player_uid, self.ai_uid, self.bstate,
            self.effect_guid, self.param)

    def battle_cards(self) -> str:
        """Resolve the metadata Battle2Cards operation through RulesPort."""
        if self.native_context or self.bstate.get("_rules_port_native_effect"):
            from rules_port.battle_effects import battle_cards
            return battle_cards(self)
        from abilities.framework.bom import _battle_cards_legacy

        return _battle_cards_legacy(
            self.game, self.session, self.db, self.handler,
            self.player_uid, self.ai_uid, self.bstate,
            self.effect_guid, self.param)


    def counter(self, target: int | None, name: str,
                counter_guid: str | None = None, amount: int = 1,
                operation: str = "add") -> tuple[int, int]:
        """Change one card/champion counter and publish its client events.

        Counter persistence differs by target: ordinary cards use the
        ``game_cards.permanent_buffs`` JSON, while champions use the
        persisted battle-state map.  Keeping both behind this operation lets
        simple effect leaves describe the rule without knowing either storage
        format or event sequence.
        """
        if target is None:
            return 0, 0
        if self.native_context or self.bstate.get("_rules_port_native_effect"):
            from rules_port.counter_effects import change_counter
            return change_counter(self, target, name, counter_guid, amount,
                                  str(operation or "add").lower())
        from abilities.framework.effects.counters import (
            add_card_counter, card_counters, change_champion_counter,
            is_champion_target, push_card_counters, push_champion_counter,
            remove_card_counters,
        )

        target = int(target)
        name = str(name or "counter").lower()
        guid = str(counter_guid or "").lower()
        operation = str(operation or "add").lower()
        amount = int(amount or 0)

        def emit_counter_added(old_value, new_value, owner_id):
            if int(new_value or 0) <= int(old_value or 0):
                return
            from rules_port.tac import _tac_attr_hash
            from abilities.framework.triggers import resolve_triggers

            resolve_triggers(
                self.db, self.handler, self.game, self.session,
                self.player_uid, self.ai_uid, self.bstate,
                "CounterAddedToCardEvent", target,
                source_owner_uid=int(owner_id or 0),
                event_tac={_tac_attr_hash("GainedCounterType"): name})

        if is_champion_target(self.handler, self.bstate, target):
            old, new = change_champion_counter(
                self.bstate, target, guid, amount,
                "set" if operation in ("clear", "removeall")
                else operation)
            if operation in ("clear", "removeall"):
                new = 0
            if guid:
                push_champion_counter(
                    self.game, self.session, self.handler,
                    self.player_uid, self.ai_uid, self.bstate,
                    target, guid, old, new)
            emit_counter_added(old, new, self.target_owner(target, 0))
            return old, new

        old = card_counters(self.db, self.session.session_id, target).get(
            name, 0)
        if operation in ("remove", "subtract"):
            new = max(0, old - amount)
            remove_card_counters(self.db, self.session.session_id, target, name)
            if new:
                add_card_counter(
                    self.db, self.session.session_id, target, name, new)
        elif operation in ("set", "clear", "removeall"):
            new = max(0, amount) if operation == "set" else 0
            remove_card_counters(self.db, self.session.session_id, target, name)
            if new:
                add_card_counter(
                    self.db, self.session.session_id, target, name, new)
        else:
            new = add_card_counter(
                self.db, self.session.session_id, target, name, amount)
        push_card_counters(
            self.game, self.session, self.db, self.handler,
            self.player_uid, self.ai_uid, target, bstate=self.bstate,
            changed_counter=name, old_value=old)
        emit_counter_added(old, new, self.target_owner(target, 0))
        return old, new

    def store_target(self, target: int | None = None) -> str:
        """Remember one resolved target for later effects in this ability."""
        target = self.resolved_target() if target is None else target
        if target is None:
            return "store targets: none"
        self.bstate.setdefault("stored_targets", {}).setdefault(
            self.ability_guid, []).append(int(target))
        return f"stored {hex(int(target))}"

    def store_name(self, target: int | None = None) -> str:
        """Remember a target card's authoritative template name."""
        target = self.resolved_target() if target is None else target
        if target is None:
            return "store name: no target"
        from pvp_db import db_card_zone_details, db_template_name
        details = db_card_zone_details(
            self.session.session_id, int(target), conn=self.db)
        name = db_template_name(details[0], conn=self.db) if details else ""
        self.bstate.setdefault("stored_names", {}).setdefault(
            self.ability_guid, []).append(name)
        return f"stored name '{name}'"

    def store_list_attr(self, list_name: str, attr_name: str, value: int,
                        *, set_list: bool = False,
                        until_end_of_turn: bool = False) -> str:
        """Persist one typed TAC list entry in resolver state."""
        list_name = str(list_name or "")
        attr_name = str(attr_name or "")
        if not list_name:
            return "store list: no list name"
        lists = self.bstate.setdefault("list_attrs", {}).setdefault(
            self.ability_guid, {})
        if set_list:
            lists[list_name] = []
        lists.setdefault(list_name, []).append({
            "name": attr_name,
            "value": int(value or 0),
            "until_end_of_turn": bool(until_end_of_turn),
        })
        return f"stored {attr_name} in {list_name}"

    def remember_keyword_powers(self) -> str:
        """Remember target abilities matching the typed keyword metadata."""
        target = self.resolved_target()
        if target is None:
            return "remember powers: no target"
        keyword = self.template_value("m_Keyword", "") or ""
        all_powers = bool(self.template_value("m_AllPowers", False))
        if self.native_context or self.bstate.get("_rules_port_native_effect"):
            from pvp_db import db_card_ability_payload
            from rules_port.trigger_discovery import ability_matches_keyword
            try:
                card_abilities = json.loads(db_card_ability_payload(
                    self.session.session_id, int(target), conn=self.db) or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                card_abilities = []
        else:
            from abilities.framework.triggers import ability_matches_keyword, _card_ability_guids
            card_abilities = _card_ability_guids(
                self.db, self.session.session_id, int(target))

        remembered = self.bstate.setdefault(
            "remembered_powers", {}).setdefault(self.ability_guid, [])
        for ability_guid in card_abilities:
            matches = (ability_matches_keyword(ability_guid, keyword)
                       if self.native_context or
                       self.bstate.get("_rules_port_native_effect") else
                       ability_matches_keyword(self.db, ability_guid, keyword))
            if all_powers or matches:
                if ability_guid not in remembered:
                    remembered.append(ability_guid)
        return f"remembered {len(remembered)} {keyword} power(s)"

    def register_trigger(self) -> str:
        """Register a typed trigger template for this battle instance."""
        target = self.resolved_target()
        template_id = self.template_value(
            "m_TriggerAbilityTemplateId", "") or ""
        if target is None or not template_id:
            return "register trigger: missing target or template"
        values = self.bstate.setdefault("registered_triggers", {}).setdefault(
            str(int(target)), [])
        if template_id not in values:
            values.append(template_id)
        return f"registered trigger {template_id[:8]} on {hex(int(target))}"

    def revoke_ability(self) -> str:
        """Remove one granted ability and publish the resulting card state."""
        target = self.resolved_target()
        if target is None:
            return "revoke: no target"
        revoked = (self.param or self.ability_guid or "").strip().lower()
        if not revoked:
            return "revoke: no ability guid in param"
        from pvp_db import db_card_abilities_state, db_set_card_abilities
        row = db_card_abilities_state(
            self.session.session_id, int(target), conn=self.db)
        if not row:
            return "revoke: target not found"
        try:
            abilities = json.loads(row[0] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            abilities = []
        if revoked in abilities:
            abilities.remove(revoked)
            db_set_card_abilities(
                self.session.session_id, int(target), json.dumps(abilities),
                conn=self.db)
            self.db.commit()
        if self.native_context or self.bstate.get("_rules_port_native_effect"):
            from rules_port.card_projection import push_card_state
            push_card_state(self.game, self.session, self.db, self.handler,
                            self.player_uid, self.ai_uid, int(target),
                            int(row[1] or 0), self.bstate)
            return f"revoked {revoked[:8]} from {hex(int(target))}"
        from abilities.framework.bom import _push_card_state

        _push_card_state(
            self.game, self.session, self.db, self.handler,
            self.player_uid, self.ai_uid, int(target), int(row[1] or 0),
            self.bstate)
        return f"revoked {revoked[:8]} from {hex(int(target))}"

    def revert_modifications(self, target: int | None = None) -> str:
        """Clear permanent stat/cost modifiers while retaining counters."""
        target = self.resolved_target() if target is None else target
        if target is None:
            return "revert: no target"
        from pvp_db import db_card_mutation_field, db_card_state_value
        raw_buffs = db_card_mutation_field(
            self.session.session_id, int(target), "permanent_buffs", conn=self.db)
        card_state = db_card_state_value(
            self.session.session_id, int(target), conn=self.db)
        row = (raw_buffs, card_state) if raw_buffs is not None else None
        try:
            data = json.loads((row[0] if row else "{}") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        if "atk" in data:
            data["atk"] = 0
        if "def" in data:
            data["def"] = 0
        from pvp_db import db_reset_card_modifiers
        db_reset_card_modifiers(
            self.session.session_id, int(target), json.dumps(data), conn=self.db)
        self.db.commit()
        if self.native_context or self.bstate.get("_rules_port_native_effect"):
            from rules_port.card_projection import push_card_state
            push_card_state(self.game, self.session, self.db, self.handler,
                            self.player_uid, self.ai_uid, int(target),
                            int(row[1] or 0) if row else 0, self.bstate)
            return f"reverted {hex(int(target))}"
        from abilities.framework.bom import _push_card_state

        _push_card_state(
            self.game, self.session, self.db, self.handler,
            self.player_uid, self.ai_uid, int(target),
            int(row[1] or 0) if row else 0, self.bstate)
        return f"reverted {hex(int(target))}"

    def remove_from_combat(self, target: int | None = None) -> str:
        """Clear combat participation flags and publish the new card state."""
        target = self.target(
            default=(self.bstate.get("player_mod_target") or
                     self.bstate.get("resolving_source_uid"))) \
            if target is None else target
        if target is None:
            return "remove from combat: no target"
        import game_engine

        target = int(target)
        clear = (game_engine.ECardStates.Attacking |
                 game_engine.ECardStates.Blocking |
                 game_engine.ECardStates.HasAttacked |
                 game_engine.ECardStates.HasBlocked)
        from pvp_db import db_card_state_value, db_set_card_state_exact
        current_state = db_card_state_value(
            self.session.session_id, target, conn=self.db)
        if current_state is None:
            return "remove from combat: card not found"
        state = int(current_state or 0) & ~int(clear)
        db_set_card_state_exact(
            self.session.session_id, target, state, conn=self.db)
        self.db.commit()
        if self.native_context or self.bstate.get("_rules_port_native_effect"):
            from rules_port.card_projection import push_card_state
            push_card_state(self.game, self.session, self.db, self.handler,
                            self.player_uid, self.ai_uid, target, state,
                            self.bstate)
            return f"removed {hex(target)} from combat"
        from abilities.framework.bom import _push_card_state

        _push_card_state(
            self.game, self.session, self.db, self.handler,
            self.player_uid, self.ai_uid, target, state, self.bstate)
        return f"removed {hex(target)} from combat"

    def update_card_state(self, target: int, *, add: int = 0,
                          remove: int = 0,
                          trigger: str | None = None,
                          commit: bool = True) -> int | None:
        """Apply card-state bits, publish ``CardUpdated``, and optionally
        dispatch the authored state-change trigger.

        This deliberately does not choose targets or interpret "each". Those
        are leaf-level orchestration concerns; this method owns only the
        shared persistence and event boundary for one card.
        """
        import game_engine

        target = int(target)
        from pvp_db import db_card_owner_zone_state, db_set_card_state_exact
        row = db_card_owner_zone_state(
            self.session.session_id, target, conn=self.db)
        if not row:
            return None
        # Older lightweight adapters return only (owner, state); the domain
        # helper returns (owner, location, state).
        previous_state = int(row[2] if len(row) > 2 else row[0] or 0)
        row_owner = row[0] if len(row) > 2 else row[1]
        state = (previous_state | int(add or 0)) & ~int(remove or 0)
        db_set_card_state_exact(
            self.session.session_id, target, state, conn=self.db)
        if commit:
            self.db.commit()
        if self.native_context or self.bstate.get("_rules_port_native_effect"):
            from rules_port.card_projection import push_card_state
            push_card_state(self.game, self.session, self.db, self.handler,
                            self.player_uid, self.ai_uid, target, state,
                            self.bstate)
            if trigger:
                self._emit_trigger(trigger, target, int(row_owner or 0))
            elif (int(remove or 0) & int(game_engine.ECardStates.Tapped)
                  and previous_state & int(game_engine.ECardStates.Tapped)):
                self._emit_trigger("CardReadiedEvent", target,
                                   int(row_owner or 0))
            return state
        from abilities.framework.bom import _push_card_state

        _push_card_state(
            self.game, self.session, self.db, self.handler,
            self.player_uid, self.ai_uid, target, state, self.bstate)
        if trigger:
            self._emit_trigger(trigger, target, int(row_owner or 0))
        elif (int(remove or 0) & int(game_engine.ECardStates.Tapped)
              and previous_state & int(game_engine.ECardStates.Tapped)):
            self._emit_trigger("CardReadiedEvent", target,
                               int(row_owner or 0))
        return state

    def replenish_resources(self) -> str:
        """Refill one controller's current resource pool."""
        import game_engine
        from rules_port.runtime_helpers import owner_uid

        owner = int(self.bstate.get("resolving_owner_id", 0) or 0)
        if self.bstate.get("pvp"):
            current_key = f"res_{owner}"
            total_key = f"res_total_{owner}"
        else:
            side = "player" if owner else "ai"
            current_key = f"{side}_resources"
            total_key = f"{side}_total_resources"
        current = int(self.bstate.get(current_key, 0) or 0)
        total = int(self.bstate.get(total_key, current) or 0)
        delta = max(0, total - current)
        self.bstate[current_key] = total
        if owner:
            self.game.player_resources = total
        else:
            self.game.ai_resources = total
        if delta:
            event = game_engine.PlayerCurrentResourcePoolChangedSessionEventArgs()
            event.player_id = owner_uid(
                owner, self.player_uid, self.ai_uid, self.bstate)
            event.operation = 1
            event.delta = delta
            event.new_value = total
            self.game._push(event)
        return f"replenish resources {current}->{total}"

    def queue_bonus_turn(self) -> str:
        """Queue the resolving controller's next turn."""
        owner = int(self.bstate.get("resolving_owner_id", 0) or 0)
        side = "player" if owner else "ai"
        self.bstate["bonus_turn"] = side
        # PvP consumes the owning player id at the turn boundary; Practice
        # still uses the side marker above as its authoritative value.
        self.bstate["bonus_turn_pid"] = owner
        return f"bonus turn queued for {side}"

    def sacrifice(self, target: int | None = None) -> str:
        """Sacrifice one resolved card through the normal death/trigger path."""
        target = self.resolved_target() if target is None else target
        if target is None:
            return "sacrifice: no target"
        if self.native_context or self.bstate.get("_rules_port_native_effect"):
            from rules_port.death_effects import kill_troop
            return kill_troop(self, target, cause="sacrifice")
        from abilities.framework.bom import _record_ability_list_target
        from abilities.framework.kill_troop import kill_troop

        kill_troop(
            self.game, self.session, self.db, self.handler,
            self.player_uid, self.ai_uid, int(target), self.bstate,
            cause="sacrifice")
        _record_ability_list_target(self.db, self.bstate, int(target))
        return f"sacrificed {hex(int(target))}"

    def summon_token(self) -> str:
        """Create tokens through the metadata token-operation helper."""
        if self.native_context or self.bstate.get("_rules_port_native_effect"):
            from rules_port.token_effects import summon_token
            return summon_token(self)
        from abilities.framework.effects.tokens import summon_token

        return summon_token(
            self.game, self.session, self.db, self.handler,
            self.player_uid, self.ai_uid, self.bstate,
            self.effect_guid, self.param)

    def summon_x_tokens(self) -> str:
        """Port SummonXTokenTroops: base amount plus source counters."""
        if self.native_context or self.bstate.get("_rules_port_native_effect"):
            from rules_port.token_effects import summon_token
            count = int(self.template_value("m_BaseAmount", 0) or 0)
            source = self.bstate.get("resolving_source_uid")
            if source is not None:
                from pvp_db import db_card_mutation_field
                try:
                    buffs = json.loads(db_card_mutation_field(
                        self.session.session_id, int(source),
                        "permanent_buffs", conn=self.db) or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    buffs = {}
                count += sum(int(value or 0) for value in
                             (buffs.get("counters", {}) if isinstance(buffs, dict) else {}).values())
            payload = {"amount": max(0, count),
                       "token_guid": self.template_value("m_CardTemplateId", ""),
                       "collection": self.template_value("m_CardCollection", "Warzone")}
            return summon_token(self, payload)
        import json
        from abilities.framework.effects.tokens import summon_token
        from abilities.framework.effects.counters import card_counters
        source = self.bstate.get("resolving_source_uid")
        base = int(self.template_value("m_BaseAmount", 0) or 0)
        counters = card_counters(self.db, self.session.session_id, source) if source else {}
        amount = max(0, base + sum(int(value or 0) for value in counters.values()))
        guid = self.template_value("m_CardTemplateId", "")
        collection = self.template_value("m_CardCollection", "Warzone")
        payload = {"amount": amount, "token_guid": str(guid or "").lower(),
                   "collection": str(collection).rsplit(".", 1)[-1]}
        return summon_token(self.game, self.session, self.db, self.handler,
                            self.player_uid, self.ai_uid, self.bstate,
                            self.effect_guid, json.dumps(payload))

    def conscript(self) -> str:
        """Conscript cards through the shared token/zone helper."""
        if self.native_context or self.bstate.get("_rules_port_native_effect"):
            from rules_port.token_effects import summon_token
            return summon_token(self, {"collection": "Hand",
                "card_filter": self.template_value("m_CardFilter", {})})
        from abilities.framework.effects.tokens import conscript_cards

        return conscript_cards(
            self.game, self.session, self.db, self.handler,
            self.player_uid, self.ai_uid, self.bstate,
            self.effect_guid, self.param)

    def load_player_deck(self) -> str:
        """Load authored player-deck cards through the shared helper."""
        if self.native_context or self.bstate.get("_rules_port_native_effect"):
            from rules_port.deck_effects import load_player_deck
            return load_player_deck(self)
        from abilities.framework.effects.tokens import load_player_deck

        return load_player_deck(
            self.game, self.session, self.db, self.handler,
            self.player_uid, self.ai_uid, self.bstate,
            self.effect_guid, self.param)

    def activate_triggered(self) -> str:
        """Activate a typed keyword trigger on the resolved target."""
        keyword = self.template_value("m_Keyword", "")
        target = self.resolved_target()
        if target is None:
            return "activate triggered: no target"
        if self.native_context or self.bstate.get("_rules_port_native_effect"):
            from pvp_db import db_card_ability_payload, db_card_owner_id
            from rules_port.resolution import resolve_port_ability
            from rules_port.trigger_discovery import ability_matches_keyword
            try:
                abilities = json.loads(db_card_ability_payload(
                    self.session.session_id, int(target), conn=self.db) or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                abilities = []
            owner = int(db_card_owner_id(
                self.session.session_id, int(target), conn=self.db) or
                self.target_owner(target, default=0) or 0)
            results = []
            for ability_guid in abilities:
                if not ability_matches_keyword(ability_guid, keyword):
                    continue
                results.append(resolve_port_ability(
                    self.handler, self.game, self.session, self.db,
                    self.player_uid, self.ai_uid, self.bstate,
                    str(ability_guid).lower(), int(target), owner,
                    target_map={}))
            result = "; ".join(str(value) for value in results if value)
            return (f"activated {keyword} on {hex(int(target))}: {result}"
                    if result else
                    f"activated {keyword}: no matching ability")
        from abilities.framework.triggers import manually_trigger_abilities
        result = manually_trigger_abilities(
            self.db, self.handler, self.game, self.session,
            self.player_uid, self.ai_uid, self.bstate,
            int(target), keyword)
        return (f"activated {keyword} on {hex(int(target))}: {result}"
                if result else f"activated {keyword}: no matching ability")

    def counter_spell(self) -> str:
        """Resolve a counter-spell operation through the trigger boundary."""
        if self.native_context or self.bstate.get("_rules_port_native_effect"):
            from rules_port.counter_effects import counter_spell
            return counter_spell(self)
        from abilities.framework.triggers import _resolve_counter_spell

        return _resolve_counter_spell(
            self.db, self.handler, self.game, self.session,
            self.player_uid, self.ai_uid, self.bstate,
            self.effect_guid, self.param, "")

    def destroy(self, target: int | None = None) -> str:
        """Destroy a resolved card or champion through the normal death path."""
        target = self.resolved_target() if target is None else target
        if target is None:
            return "destroy: no target"
        if self.native_context or self.bstate.get("_rules_port_native_effect"):
            from rules_port.death_effects import kill_troop
            return kill_troop(self, target, cause="effect")
        from abilities.framework.kill_troop import kill_troop

        target = int(target)
        from pvp_db import db_card_location
        card_row = db_card_location(self.session.session_id, target, conn=self.db)
        if not card_row:
            from abilities.framework.bom import _controller_id_for_target

            target_owner = _controller_id_for_target(
                self.db, self.session, self.handler, self.bstate, target)
            if target_owner is not None:
                if self.bstate.get("pvp"):
                    from services.tournament_game import _pvp_end_game

                    pids = [int(pid) for pid in
                            (self.bstate.get("pids") or [])]
                    winner = next(
                        (pid for pid in pids if pid != target_owner), None)
                    if winner is not None:
                        _pvp_end_game(
                            self.session, self.bstate, winner,
                            target_owner, "champion destroyed by card effect")
                        return f"destroyed champion {hex(target)}"
                else:
                    import commands

                    winner_uid = (self.player_uid if target_owner == 0
                                  else self.ai_uid)
                    loser_uid = (self.ai_uid if target_owner == 0
                                 else self.player_uid)
                    commands.push_battle_game_end(
                        handler=self.handler, session=self.session,
                        winners=[winner_uid], losers=[loser_uid])
                    if hasattr(self.handler, "_campaign_gameend"):
                        self.handler._campaign_gameend(
                            self.session, won=(target_owner == 0))
                    return f"destroyed champion {hex(target)}"
        kill_troop(
            self.game, self.session, self.db, self.handler,
            self.player_uid, self.ai_uid, target, self.bstate, cause="effect")
        return f"destroyed {hex(target)}"

    def lose_thresholds(self, names) -> str:
        """Remove authored shard thresholds from the target controller."""
        import game_engine
        from rules_port.runtime_helpers import owner_uid

        target = self.target(default=(self.bstate.get("player_mod_target")
                                      or self.bstate.get("resolving_source_uid")))
        owner = self.target_owner(
            target, default=self.bstate.get("resolving_owner_id", 0))
        colors = {"Colorless": 1, "Blood": 4, "Ruby": 8,
                  "Sapphire": 16, "Wild": 32, "Diamond": 64}
        changed = 0
        for name in names or ():
            color = colors.get(str(name).split(".")[-1], 0)
            if not color:
                continue
            key = (f"thresh_{owner}" if self.bstate.get("pvp") else
                   ("player_threshold" if owner else "ai_threshold"))
            thresholds = self.bstate.setdefault(key, {})
            old = int(thresholds.get(color, thresholds.get(str(color), 0)) or 0)
            if old <= 0:
                continue
            thresholds[color] = 0
            thresholds.pop(str(color), None)
            changed += old
            event = game_engine.PlayerResourceThresholdChangedSessionEventArgs()
            event.player_id = owner_uid(
                owner, self.player_uid, self.ai_uid, self.bstate)
            event.color = color
            event.operation = 2
            event.delta = old
            event.new_value = 0
            self.game._push(event)
        return f"lost {changed} threshold(s)"

    def swap_health(self) -> str:
        """Exchange the resolving controller's champion health values."""
        import game_engine

        from rules_port.runtime_helpers import owner_uid, pvp_opponent_pid

        owner = int(self.bstate.get("resolving_owner_id", 0) or 0)
        if self.bstate.get("pvp"):
            target = self.resolved_target()
            target_pid = pvp_opponent_pid(self.bstate, owner)
            if target is not None:
                for pid, cuid in (self.bstate.get("champ_map") or {}).items():
                    if int(cuid) == int(target):
                        target_pid = int(pid)
                        break
            if target_pid is None:
                return "swap health: no opposing champion"
            health_map = self.bstate.get("pvp_health_map") or {}
            key_a = health_map.get(owner, f"hp_{owner}")
            key_b = health_map.get(target_pid, f"hp_{target_pid}")
            a = int(self.bstate.get(key_a, 20))
            b = int(self.bstate.get(key_b, 20))
            self.bstate[key_a], self.bstate[key_b] = b, a
            self.game.player_health = self.bstate[key_a]
            self.game.ai_health = self.bstate[key_b]
            for pid, old, new in ((owner, a, b), (target_pid, b, a)):
                event = game_engine.ChampionHealthChangedSessionEventArgs()
                event.player_id = owner_uid(
                    pid, self.player_uid, self.ai_uid, self.bstate)
                event.old_damage_value = old
                event.new_damage_value = new
                self.game._push(event)
            return f"swapped health {owner}<->{target_pid}"

        profile = getattr(self.handler, "user_profile", None) or {}
        other = 0 if owner else int(profile.get("id", 0) or 0)
        key_a, key_b = (("player_health", "ai_health") if owner else
                        ("ai_health", "player_health"))
        a = int(self.bstate.get(key_a, getattr(self.game, key_a, 20)))
        b = int(self.bstate.get(key_b, getattr(self.game, key_b, 20)))
        self.bstate[key_a], self.bstate[key_b] = b, a
        setattr(self.game, key_a, b)
        setattr(self.game, key_b, a)
        for owner_id, old, new in ((owner, a, b), (other, b, a)):
            event = game_engine.ChampionHealthChangedSessionEventArgs()
            event.player_id = owner_uid(
                owner_id, self.player_uid, self.ai_uid, self.bstate)
            event.old_damage_value = old
            event.new_damage_value = new
            self.game._push(event)
        return "swapped champion health"

    def _legacy(self, function_name: str):
        """Invoke one transitional orchestration implementation.

        Complex effects keep their established behavior in ``bom.py`` while
        the resolver and simple leaves move to a typed context.  Importing at
        call time avoids the context/BOM registration cycle and makes the
        boundary explicit in each named operation below.
        """
        if ((self.native_context or
             (getattr(self.session, "_rules_port_session", None) is not None and
              self.bstate.get("_rules_port_native_effect"))) and
                not self.bstate.get("_rules_port_allow_legacy_helper")):
            raise RuntimeError(
                "RulesPort effect called legacy helper "
                f"{function_name}; add a native implementation")
        from abilities.framework import bom

        return getattr(bom, function_name)(
            self.game, self.session, self.db, self.handler,
            self.player_uid, self.ai_uid, self.bstate,
            self.effect_guid, self.param)

    def _emit_authored_event(self, event_type: str,
                             target: int | None = None) -> str:
        """Dispatch one metadata-defined ability event.

        ``FireEventEffectTemplate`` is the authored bridge for keyword-like
        events such as Fateweave, Illuminate, Verdict, and Prophecy.  Keep
        the dispatch here so the effect uses the same trigger/condition/
        target machinery as ordinary game events in both PVE and PVP.
        """
        source = self.bstate.get("resolving_source_uid")
        if source is None:
            source = target
        if source is None:
            return f"{event_type}: no source"
        source = int(source)
        owner = self.target_owner(source, default=None)
        if owner is None:
            owner = int(self.bstate.get("resolving_owner_id", 0) or 0)
        result = self._emit_trigger(
            str(event_type).rsplit(".", 1)[-1], source, int(owner),
            target_card_id=(int(target) if target is not None and
                            int(target) != source else None))
        return f"fired {str(event_type).rsplit('.', 1)[-1]}" + (
            f": {result}" if result else "")

    def create_and_cast_spell(self):
        if not (self.native_context or self.bstate.get("_rules_port_native_effect")):
            return self._legacy("_create_and_cast_spell_legacy")
        from rules_port.effects import create_and_cast_spell
        return create_and_cast_spell(self)

    def verdict(self):
        return self._emit_authored_event("VerdictEvent")

    def grant_ability(self):
        if not (self.native_context or self.bstate.get("_rules_port_native_effect")):
            return self._legacy("_grant_ability_legacy")
        target = self.bstate.get("grant_target")
        from pvp_db import db_card_grant_info
        if target is None or not db_card_grant_info(
                self.session.session_id, int(target), conn=self.db):
            return "grant: target card not found"
        from rules_port.effects import grant_ability
        return grant_ability(self)

    def copy_ability(self):
        if self.native_context or self.bstate.get("_rules_port_native_effect"):
            from rules_port.effects import _copy_ability
            return _copy_ability(self)
        return self._legacy("_copy_ability_legacy")

    def play_card(self):
        if not (self.native_context or self.bstate.get("_rules_port_native_effect")):
            return self._legacy("_play_card_legacy")
        target = self.resolved_target()
        if target is None:
            target = self.bstate.get("resolving_source_uid")
        if target is None:
            return "play for free: no source"
        from pvp_db import db_card_zone_details, db_card_with_template
        row = db_card_zone_details(
            self.session.session_id, int(target), conn=self.db)
        if not row:
            return "play for free: target not found"
        card_row = db_card_with_template(
            self.session.session_id, int(target), conn=self.db)
        card_type = card_row[1] if card_row else ""
        if "choice" in str(card_type or "").lower():
            # A choice token is not a spell/permanent to put on the chain.
            # Playing it consumes the temporary token and resolves its
            # authored ability (for example Choose Blood's threshold gain).
            from rules_port.choice_effects import (
                _play_choice_card, _resolve_choice_card_abilities)
            owner_id = int(self.target_owner(
                target, default=self.bstate.get("resolving_owner_id", 0)) or 0)
            _play_choice_card(self, int(target), owner_id)
            _resolve_choice_card_abilities(
                self, int(target), self.bstate.get("resolving_source_uid"),
                owner_id)
            return f"played choice {int(target)} for free"
        if "resource" in str(card_type or "").lower():
            from rules_port.host_mutations import play_free_resource_card
            return play_free_resource_card(
                self.handler, self.game, self.session, self.db,
                self.player_uid, self.ai_uid, self.bstate, int(target))
        if int(target) != int(self.bstate.get(
                "resolving_source_uid", target) or target):
            raise RuntimeError(
                "RulesPort free-play target is not the ability source")
        from rules_port.host_mutations import queue_free_played_card
        return queue_free_played_card(
            self.handler, self.game, self.session, self.db,
            self.player_uid, self.ai_uid, self.bstate, int(target),
            int(self.target_owner(target,
                                  default=self.bstate.get("resolving_owner_id", 0))
                or 0), row[0], card_type)

    def fire_event(self):
        event_type = self.template_value("m_TriggerType", "") or ""
        event_type = str(event_type).rsplit(".", 1)[-1]
        if event_type in ("", "GenericEvent"):
            # Older records use GenericEvent for the named Prophesied
            # operation.  The effect template name is the typed fallback.
            name = str(self.template_value("m_Name", "") or "")
            if name.lower().startswith("fire") and name.endswith("Event"):
                event_type = name[4:]
        if not event_type:
            return "fire event: no authored event type"
        return self._emit_authored_event(event_type, self.resolved_target())

    def activate_ability(self):
        """Enter a child ability through the common typed resolver.

        ``resolve_ability`` normally handles this effect before a leaf is
        invoked.  Retaining this path for direct/legacy callers is important,
        but flat-walking child BOM rows here would bypass child targets,
        conditions, continuations, and EffectContext injection.
        """
        child_guid = str(self.param or "").lower()
        if not child_guid or child_guid == "00000000-0000-0000-0000-000000000000":
            return "activate ability: no child"
        source_uid = self.bstate.get("resolving_source_uid")
        owner_id = self.target_owner(
            self.resolved_target(),
            default=self.bstate.get("resolving_owner_id", 0))
        if self.native_context or self.bstate.get("_rules_port_native_effect"):
            # Child abilities must re-enter the RulesPort lifecycle so their
            # instance/continuation state and native effect dispatch are not
            # lost at this nested boundary.
            from rules_port.resolution import resolve_port_ability
            target_map = {}
            selected = self.bstate.get("selected_choice_uid")
            if selected is not None:
                target_map[0] = (int(selected),)
            return resolve_port_ability(
                self.handler, self.game, self.session, self.db,
                self.player_uid, self.ai_uid, self.bstate, child_guid,
                source_uid, owner_id, target_map=target_map,
                variables=self.bstate.get("ability_variables") or {})
        from abilities.framework.resolution import resolve_ability
        return resolve_ability(
            self.handler, self.game, self.session, self.db,
            self.player_uid, self.ai_uid, self.bstate, child_guid,
            source_uid, owner_id, target_map={},
            variables=self.bstate.get("ability_variables") or {})

    def tac(self):
        """Apply one metadata-defined TAC operation.

        TAC operations are the client's generic escape hatch for mechanics
        that do not need a dedicated effect-template class.  Keep the
        operation name and arguments in Records, while using the same typed
        target, owner, persistence, and event helpers as ordinary effects.
        """
        from rules_port.tac import (tac_function, tac_guid, tac_int, tac_string)

        serialized = self.template_value("m_SerializedTAC", None)
        if isinstance(serialized, dict):
            serialized = serialized.get("data")
        serialized = serialized or self.param
        function = tac_function(serialized)
        target = self.resolved_target()

        if function == "AppendToList":
            list_name = tac_string(serialized, "ListName")
            where = tac_string(serialized, "Where", "ThisTurnsData")
            data = {"source_uid": self.bstate.get("resolving_source_uid")}
            from rules_port.tac import decode_tac_tree, _tac_attr_hash
            tree = decode_tac_tree(serialized)
            nested = tree.get(_tac_attr_hash("DataToAppend"))
            ready = tac_int(serialized, "ReadyYourTroops", 0)
            if isinstance(nested, dict):
                ready = nested.get(_tac_attr_hash("ReadyYourTroops"), ready)
            if ready:
                data["ReadyYourTroops"] = 1
            # The extracted typed template is authoritative for nested TAC
            # data.  Keep unknown fields rather than making a card-specific
            # interpretation of a future list entry.
            if isinstance(nested, dict):
                data.update({str(k): v for k, v in nested.items()})
            lists = self.bstate.setdefault("list_attrs", {})
            ability_lists = lists.setdefault(self.ability_guid, {})
            entries = ability_lists.setdefault(list_name, [])
            entries.append({"where": where, **data})
            if list_name == "ExtraCombatsThisTurn":
                owner = self.target_owner(
                    target, self.bstate.get("resolving_owner_id", 0))
                owner_key = (str(owner) if self.bstate.get("pvp") else
                             ("ai" if int(owner or 0) == 0 else "player"))
                extra = self.bstate.setdefault(
                    "extra_combats_this_turn", {}).setdefault(owner_key, [])
                extra.append({
                    "ready_your_troops": bool(ready),
                    "source_uid": data.get("source_uid"),
                })
            return f"appended {list_name}"

        if function == "CycleCardArt":
            if target is None:
                return "cycle card art: no target"
            from domain.events import CycleCardArtSessionEventArgs
            event = CycleCardArtSessionEventArgs()
            event.session_card_id = game_engine.SessionCardId(
                game_engine.UID(int(target)))
            self.game._push(event)
            return f"cycled art {hex(int(target))}"

        if function == "DepleteResources":
            owner = self.target_owner(target, self.bstate.get(
                "resolving_owner_id", 0))
            owner = int(owner or 0)
            if self.bstate.get("pvp"):
                key = f"res_{owner}"
            else:
                key = "player_resources" if owner else "ai_resources"
            current = int(self.bstate.get(key, 0) or 0)
            self.bstate[key] = 0
            if owner:
                self.game.player_resources = 0
            else:
                self.game.ai_resources = 0
            if current:
                from rules_port.runtime_helpers import owner_uid
                event = game_engine.PlayerCurrentResourcePoolChangedSessionEventArgs()
                event.player_id = owner_uid(owner, self.player_uid,
                                            self.ai_uid, self.bstate)
                event.operation = 2
                event.delta = current
                event.new_value = 0
                self.game._push(event)
            return f"depleted {owner} resources ({current})"

        if function == "Escalate":
            owner = int(self.bstate.get("resolving_owner_id", 0) or 0)
            side = "ai" if owner == 0 else "player"
            key = f"{side}_escalation_uses"
            if self.bstate.get("_esc_counted_this_resolution"):
                return "escalate (already counted by ESC leaf)"
            self.bstate[key] = int(self.bstate.get(key, 0) or 0) + 1
            self.bstate["_esc_counted_this_resolution"] = True
            return f"escalate {side} (uses={self.bstate[key]})"

        if function == "ForgetAllCards":
            self.bstate.setdefault("stored_targets", {}).pop(
                self.ability_guid, None)
            self.bstate.setdefault("ability_lists", {}).pop(
                self.ability_guid, None)
            return "forgot stored cards"

        if function in ("GainTargetsThresholds", "GainTargetsProvidedThresholds"):
            if target is None:
                return "gain thresholds: no target"
            owner = int(self.bstate.get("resolving_owner_id", 0) or 0)
            amounts = {}
            if function == "GainTargetsProvidedThresholds":
                from pvp_db import db_card_ability_payload, db_ability_effect_rows
                payload = db_card_ability_payload(
                    self.session.session_id, int(target), conn=self.db)
                try:
                    ability_guids = json.loads(payload or "[]") if payload else []
                except (TypeError, ValueError, json.JSONDecodeError):
                    ability_guids = []
                from game_engine import SHARD_TO_FLAG
                from rules_port.fields import effect_template
                for ability_guid in ability_guids:
                    effects = db_ability_effect_rows(
                        str(ability_guid).lower(), conn=self.db)
                    for effect_guid, effect_type in effects:
                        if effect_type != "CardModifierAbilityEffectTemplate":
                            continue
                        modifier = (effect_template(self.ability, effect_guid) or {}).get(
                            "m_Modifier") or {}
                        if str(modifier.get("_t", "")).rsplit(".", 1)[-1] != \
                                "ThresholdModifier":
                            continue
                        color = SHARD_TO_FLAG.get(str(
                            modifier.get("m_ThresholdColor", "")).lower())
                        if color:
                            amounts[color] = amounts.get(color, 0) + 1
            else:
                from pvp_db import db_card_template_threshold_subtype
                row = db_card_template_threshold_subtype(
                    self.session.session_id, int(target), conn=self.db)
                from rules_port.targeting import shards_from_threshold
                for color in shards_from_threshold(row[0] if row else ""):
                    amounts[color] = amounts.get(color, 0) + 1
            from rules_port.runtime_helpers import owner_uid
            for color, amount in amounts.items():
                key = (f"thresh_{owner}" if self.bstate.get("pvp") else
                       ("player_threshold" if owner else "ai_threshold"))
                values = self.bstate.setdefault(key, {})
                old = int(values.get(color, values.get(str(color), 0)) or 0)
                values[color] = old + int(amount)
                event = game_engine.PlayerResourceThresholdChangedSessionEventArgs()
                event.player_id = owner_uid(owner, self.player_uid,
                                            self.ai_uid, self.bstate)
                event.color = color
                event.operation = 1
                event.delta = int(amount)
                event.new_value = values[color]
                self.game._push(event)
            return f"gained {sum(amounts.values())} threshold(s)"

        if function == "MoveInDeck":
            if target is None:
                return "move in deck: no target"
            from pvp_db import db_card_owner_location_position, db_ordered_zone_uids
            row = db_card_owner_location_position(
                self.session.session_id, int(target), conn=self.db)
            if not row or row[1] != "deck":
                return "move in deck: target not in deck"
            offset = tac_int(serialized, "Offset", 0)
            deck = [int(value[0]) for value in db_ordered_zone_uids(
                self.session.session_id, int(row[0]), "deck", conn=self.db)]
            if int(target) not in deck or not deck:
                return "move in deck: target missing"
            old = deck.index(int(target))
            new = max(0, min(len(deck) - 1, old - int(offset)))
            deck.insert(new, deck.pop(old))
            from pvp_db import db_set_card_positions
            db_set_card_positions([
                (self.session.session_id, uid, index)
                for index, uid in enumerate(deck)], conn=self.db)
            self.db.commit()
            return f"moved {hex(int(target))} in deck {old}->{new}"

        if function == "RemoveFromStoredTargetsAbility":
            if target is None:
                return "remove stored target: no target"
            stored = self.bstate.setdefault("stored_targets", {}).setdefault(
                self.ability_guid, [])
            try:
                stored.remove(int(target))
            except ValueError:
                pass
            return f"removed {hex(int(target))} from stored targets"

        if function == "ReplaceCardInCollection":
            if target is None:
                target = self.bstate.get("resolving_source_uid")
            new_template = tac_guid(serialized)
            if target is None or not new_template:
                return "replace collection: missing target/template"
            from pvp_db import db_card_collection_info
            row = db_card_collection_info(
                self.session.session_id, int(target), conn=self.db)
            original = row[1] if row and row[1] else row[2] if row else ""
            user_id = int(row[0]) if row else int(
                self.bstate.get("resolving_owner_id", 0) or 0)
            if original:
                from profile_db import db_remove_collection
                db_remove_collection(user_id, original, conn=self.db)
            from profile_db import db_add_collection
            db_add_collection(user_id, new_template, conn=self.db)
            self.db.commit()
            return f"replaced collection card with {new_template[:8]}"

        if function == "RevokeBaseAbilities":
            if target is None:
                return "revoke base abilities: no target"
            from rules_port.ability_mutations import revoke_base_abilities
            return revoke_base_abilities(self, int(target))

        if function == "ShiftAbility":
            source = self.bstate.get("player_shift_source")
            destination = self.bstate.get("player_shift_target") or target
            guid = tac_guid(serialized)
            if not source or not destination or not guid:
                return "shift: missing source/target/ability"
            from rules_port.ability_mutations import shift_ability
            return shift_ability(self, int(source), int(destination), guid)

        if function == "Tame":
            if target is None:
                return "tame: no target"
            # Tame is a typed capture operation: the shared IntAttrModifier
            # path owns voiding, capture records, and zone-exit triggers.
            self.bstate["player_mod_target"] = int(target)
            payload = {"property": "intattr", "attribute": "Tamed",
                       "amount": 1}
            if self.native_context or self.bstate.get(
                    "_rules_port_native_effect"):
                from rules_port.effects import _card_modifier
                return _card_modifier(self, {"param": json.dumps(payload)})
            from abilities.framework.bom import _apply_card_modifier
            return _apply_card_modifier(
                self.game, self.session, self.db, self.handler,
                self.player_uid, self.ai_uid, self.bstate, self.effect_guid,
                json.dumps(payload))

        if self.native_context or self.bstate.get("_rules_port_native_effect"):
            raise RuntimeError(
                "RulesPort TAC has no native handler: " + str(function))
        return self._legacy("_tac_legacy")

    def block(self):
        if not (self.native_context or self.bstate.get("_rules_port_native_effect")):
            from abilities.framework.effects.combat import _block_legacy
            return _block_legacy(
                self.game, self.session, self.db, self.handler,
                self.player_uid, self.ai_uid, self.bstate,
                self.effect_guid, self.param)
        from rules_port.combat_effects import block
        return block(self)

    def double_choice(self):
        if self.native_context or self.bstate.get("_rules_port_native_effect"):
            from rules_port.choice_effects import double_choice
            return double_choice(self)
        from abilities.framework.effects.choices import _double_choice_legacy

        return _double_choice_legacy(
            self.game, self.session, self.db, self.handler,
            self.player_uid, self.ai_uid, self.bstate,
            self.effect_guid, self.param)
