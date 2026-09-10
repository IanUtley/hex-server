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

    @classmethod
    def from_legacy(cls, game, session, db, handler, pl_t, ai_t, bstate,
                    effect_guid, param="") -> "EffectContext":
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
        )

    @property
    def ability_guid(self) -> str:
        return str(self.bstate.get("resolving_ability") or "").lower()

    @property
    def ability(self):
        """Return the active metadata-backed builder, when resolving."""
        return self.bstate.get("_ability_builder")

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
        from .fields import effect_field

        return effect_field(
            self.db, self.bstate, self.effect_guid, field_name, default)

    def template_value(self, field_name: str, default: Any = None) -> Any:
        """Read a non-numeric typed field from the effect template."""
        if self.ability is not None:
            return self.ability.template_value(
                self.db, self.bstate, field_name,
                effect=self.effect_guid, default=default)
        from .fields import effect_template_value

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
            from .fields import resolve_field

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
        row = self.db.execute(
            "SELECT user_id FROM game_cards WHERE session_id=? AND card_uid=?",
            (self.session.session_id, int(target))).fetchone()
        if row:
            return int(row[0])
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
        for _ in range(count):
            if owner == 0:
                import ai
                ai.ai_draw_card(self.handler, self.game, self.session,
                                self.ai_uid, self.bstate)
                continue
            from ._shared import owner_uid

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

    def put_top_into_hand(self) -> str:
        """Put typed-count deck cards into the caster's hand."""
        import game_engine

        from ._shared import owner_uid
        from .bom import _deck_owner_for_target

        count = self.value("m_InputValue", default=1)
        target = self.resolved_target()
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
            row = self.db.execute(
                "SELECT id, card_uid, card_template_id, template_guid "
                "FROM game_cards WHERE session_id=? AND user_id=? "
                "AND location='deck' ORDER BY position LIMIT 1",
                (self.session.session_id, deck_owner)).fetchone()
            if not row:
                break
            scid = game_engine.SessionCardId(game_engine.UID(row[1]))
            self.db.execute(
                "UPDATE game_cards SET user_id=?, location='hand', "
                "position=100 WHERE id=?", (hand_owner, row[0]))
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

        from db import db_discard_card
        from ._shared import owner_uid
        from .bom import _deck_owner_for_target
        from .triggers import resolve_triggers

        count = self.value("m_Amount", default=1)
        target = self.resolved_target()
        deck_owner = (_deck_owner_for_target(
            self.db, self.handler, self.session, self.bstate, target)
            if target is not None else None)
        if deck_owner is None:
            deck_owner = 0
        discard_owner = owner_uid(deck_owner, self.player_uid, self.ai_uid,
                                  self.bstate)
        total = 0
        for _ in range(max(0, int(count))):
            row = self.db.execute(
                "SELECT card_uid, template_guid FROM game_cards "
                "WHERE session_id=? AND user_id=? AND location='deck' "
                "ORDER BY position LIMIT 1",
                (self.session.session_id, deck_owner)).fetchone()
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
            resolve_triggers(
                self.db, self.handler, self.game, self.session,
                self.player_uid, self.ai_uid, self.bstate,
                "CardEnteredZoneEvent", int(row[0]),
                source_owner_uid=deck_owner,
                event_source_collection="deck",
                event_destination_collection="discard",
                event_previous_state=0)
            resolve_triggers(
                self.db, self.handler, self.game, self.session,
                self.player_uid, self.ai_uid, self.bstate,
                "CardDiscardedEvent", int(row[0]),
                source_owner_uid=deck_owner,
                event_source_collection="deck",
                event_destination_collection="discard",
                event_previous_state=0)
            total += 1
        return f"bury {total} cards"

    def void_card(self) -> str:
        """Move a resolved card to Void and publish its exit relationship."""
        import game_engine

        from ._shared import owner_uid
        from .triggers import resolve_triggers

        target = self.target()
        if target is None:
            target = self.resolved_target()
        if target is None:
            return "void: no target"
        target = int(target)
        row = self.db.execute(
            "SELECT user_id FROM game_cards WHERE session_id=? AND card_uid=?",
            (self.session.session_id, target)).fetchone()
        if not row:
            return "void: target not found"
        owner = owner_uid(row[0], self.player_uid, self.ai_uid, self.bstate)
        self.db.execute(
            "UPDATE game_cards SET location='void', position=0 "
            "WHERE session_id=? AND card_uid=?",
            (self.session.session_id, target))
        self.db.commit()
        scid = game_engine.SessionCardId(game_engine.UID(target))
        tpl_row = self.db.execute(
            "SELECT template_guid FROM game_cards WHERE session_id=? "
            "AND card_uid=?", (self.session.session_id, target)).fetchone()
        tpl_guid = tpl_row[0] if tpl_row else None
        _tpl, ct, _name, cost, atk, defense, gem = \
            self.handler._card_full_data(self.game, scid, tpl_guid)
        self.game.push_card_moved(
            scid, owner, game_engine.ECardCollections.Void,
            game_engine.ECardLocations.Top, 0)
        self.game.push_card_updated(
            scid, owner, game_engine.ECardCollections.Void, ct,
            template_id=tpl_guid, attack=atk, defense=defense,
            cost=cost, gems=gem)
        resolve_triggers(
            self.db, self.handler, self.game, self.session,
            self.player_uid, self.ai_uid, self.bstate,
            "CardExitedZoneEvent", target, source_owner_uid=row[0])

        source = self.bstate.get("resolving_source_uid")
        if source is not None:
            self.bstate.setdefault("voided_by", {}).setdefault(
                str(int(source)), []).append(target)
            source_row = self.db.execute(
                "SELECT template_guid, user_id, location FROM game_cards "
                "WHERE session_id=? AND card_uid=?",
                (self.session.session_id, int(source))).fetchone()
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
        from .effects.damage import deal_damage

        return deal_damage(
            self.game, self.session, self.db, self.handler,
            self.player_uid, self.ai_uid, self.bstate, target, int(amount))

    def discard(self, target: int | None = None) -> str:
        """Move a hand/choosing card to discard and publish its projections."""
        import game_engine

        target = self.resolved_target() if target is None else target
        if target is None:
            return "discard: no target"
        row = self.db.execute(
            "SELECT template_guid, card_template_id, user_id, location, "
            "card_state FROM game_cards WHERE session_id=? AND card_uid=?",
            (self.session.session_id, int(target))).fetchone()
        if not row:
            return f"discard: target {hex(int(target))} not found"
        if str(row[3]).lower() not in ("hand", "choosing"):
            return f"discard: target {hex(int(target))} is in {row[3]}"

        from db import db_discard_card
        from ._shared import owner_uid

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
        from .triggers import resolve_triggers

        resolve_triggers(
            self.db, self.handler, self.game, self.session,
            self.player_uid, self.ai_uid, self.bstate,
            "CardDiscardedEvent", int(target), source_owner_uid=owner_id,
            event_source_collection=row[3],
            event_destination_collection="discard",
            event_previous_state=int(row[4] or 0))
        return f"discarded {hex(int(target))}"

    def discard_or_sacrifice(self) -> str:
        """Apply the destination-dependent discard/sacrifice operation."""
        target = self.resolved_target()
        if target is None:
            return "discard or sacrifice: no target"
        row = self.db.execute(
            "SELECT location FROM game_cards WHERE session_id=? "
            "AND card_uid=?", (self.session.session_id, int(target))).fetchone()
        if not row:
            return "discard or sacrifice: target not found"
        if row[0] == "warzone":
            return self.sacrifice(target)
        if row[0] == "hand":
            return self.discard(target)
        return f"discard or sacrifice: ignored {row[0]}"

    def tunnel(self) -> str:
        """Move the resolved card underground and publish its representation."""
        target = self.resolved_target()
        if target is None:
            return "tunnel: no target"
        self.db.execute(
            "UPDATE game_cards SET location='underground', position=9999 "
            "WHERE session_id=? AND card_uid=?",
            (self.session.session_id, int(target)))
        self.db.commit()
        from .effects.utility import _push_card_in_zone

        _push_card_in_zone(
            self.game, self.session, self.db, self.handler,
            self.player_uid, self.ai_uid, self.bstate, int(target),
            "underground")
        return f"tunneled {hex(int(target))}"

    def create_matching_token(self) -> str:
        """Create copies of the resolved target using typed effect fields."""
        from .effects.utility import _create_matching_target

        target = self.resolved_target()
        if target is None:
            return "matching token: no target"
        count = self.value("m_InputValue", default=1)
        collection = self.template_value("m_CardCollection", "Warzone")
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
        row = self.db.execute(
            "SELECT template_guid FROM game_cards WHERE session_id=? "
            "AND card_uid=?", (self.session.session_id, int(target))).fetchone()
        if not row:
            return "transform replica: target not found"
        from .transform import transform_card

        transform_card(
            self.handler, self.game, self.session,
            self.player_uid, self.ai_uid, int(target), row[0],
            keep_zone=True, bstate=self.bstate)
        return f"replicated {hex(int(target))}"

    def transform_self(self) -> str:
        """Transform the resolving source using its authored templates."""
        import random

        source = self.bstate.get("resolving_source_uid")
        if source is None:
            return "transform self: no source"
        from .bom import _linked_template_guids_from_metadata
        from .transform import transform_card

        linked = _linked_template_guids_from_metadata(self.db, self.bstate)
        if not linked:
            return "transform self: no template"
        new_template = random.choice(linked)
        transform_card(
            self.handler, self.game, self.session,
            self.player_uid, self.ai_uid, int(source), new_template,
            bstate=self.bstate)
        return f"transformed self -> {new_template[:8]}"

    def transform_card_random(self) -> str:
        """Run the authored random-filter transform operation."""
        from .bom import _transform_card_at_random_legacy

        return _transform_card_at_random_legacy(
            self.game, self.session, self.db, self.handler,
            self.player_uid, self.ai_uid, self.bstate,
            self.effect_guid, self.param)

    def transform_card(self) -> str:
        """Run the authored direct-template transform operation."""
        from .bom import _transform_card_legacy

        return _transform_card_legacy(
            self.game, self.session, self.db, self.handler,
            self.player_uid, self.ai_uid, self.bstate,
            self.effect_guid, self.param)

    def create_token_copy(self) -> str:
        """Run the authored token-copy operation and creation triggers."""
        from .bom import _create_token_copy_legacy

        return _create_token_copy_legacy(
            self.game, self.session, self.db, self.handler,
            self.player_uid, self.ai_uid, self.bstate,
            self.effect_guid, self.param)

    def destroy_by_defense(self) -> str:
        """Destroy warzone troops that fail the authored defense survival roll."""
        import random

        from .kill_troop import kill_troop

        rows = self.db.execute(
            "SELECT card_uid FROM game_cards WHERE session_id=? "
            "AND location='warzone' AND card_type LIKE '%Troop%'",
            (self.session.session_id,)).fetchall()
        destroyed = 0
        for (uid,) in rows:
            stat_row = self.db.execute(
                "SELECT ct.defense, gc.card_defense_mod "
                "FROM game_cards gc JOIN card_templates ct "
                "ON ct.guid = gc.template_guid "
                "WHERE gc.session_id=? AND gc.card_uid=?",
                (self.session.session_id, int(uid))).fetchone()
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
        from .fields import ability_record
        from .statics import _leaf_numeric_value

        payload = dict(param or {})
        metadata = metadata or {}
        if metadata.get("input_variable"):
            payload.setdefault("input_variable", metadata["input_variable"])
        if metadata.get("input_value") and not payload.get("amount"):
            payload["amount"] = metadata["input_value"]
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

        from .bom import _champion_target_uid, _opposing_champion_uid

        param = dict(param or {})
        text = str(param.get("text") or "")
        target = self.modifier_target()
        if target is None:
            target = _champion_target_uid(
                self.handler, self.bstate, self.db, self.session)
        if target is None:
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
                row = self.db.execute(
                    "SELECT ct.defense, gc.card_defense_mod FROM game_cards gc "
                    "JOIN card_templates ct ON ct.guid = gc.template_guid "
                    "WHERE gc.session_id=? AND gc.card_uid=?",
                    (self.session.session_id, int(source_uid))).fetchone()
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
        from .stat_mod import apply_card_stat_mod

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
        from .bom import _card_modifier_legacy

        return _card_modifier_legacy(
            self.game, self.session, self.db, self.handler,
            self.player_uid, self.ai_uid, self.bstate,
            self.effect_guid, self.param)

    def move_card_to_zone(self) -> str:
        """Run the metadata zone transition and its client-event contract."""
        from .bom import _move_card_to_zone_legacy

        return _move_card_to_zone_legacy(
            self.game, self.session, self.db, self.handler,
            self.player_uid, self.ai_uid, self.bstate,
            self.effect_guid, self.param)

    def reveal_cards(self) -> str:
        """Run the metadata reveal and client prompt/event contract."""
        from .bom import _reveal_cards_legacy

        return _reveal_cards_legacy(
            self.game, self.session, self.db, self.handler,
            self.player_uid, self.ai_uid, self.bstate,
            self.effect_guid, self.param)

    def battle_cards(self) -> str:
        """Resolve the metadata Battle2Cards combat operation."""
        from .bom import _battle_cards_legacy

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
        from .effects.counters import (
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
            from .tac import _tac_attr_hash
            from .triggers import resolve_triggers

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
        row = self.db.execute(
            "SELECT ct.name FROM game_cards gc JOIN card_templates ct "
            "ON ct.guid = gc.template_guid WHERE gc.session_id=? "
            "AND gc.card_uid=?",
            (self.session.session_id, int(target))).fetchone()
        name = row[0] if row else ""
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
        from .triggers import ability_matches_keyword, _card_ability_guids

        remembered = self.bstate.setdefault(
            "remembered_powers", {}).setdefault(self.ability_guid, [])
        for ability_guid in _card_ability_guids(
                self.db, self.session.session_id, int(target)):
            if all_powers or ability_matches_keyword(
                    self.db, ability_guid, keyword):
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
        row = self.db.execute(
            "SELECT card_abilities, card_state FROM game_cards "
            "WHERE session_id=? AND card_uid=?",
            (self.session.session_id, int(target))).fetchone()
        if not row:
            return "revoke: target not found"
        try:
            abilities = json.loads(row[0] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            abilities = []
        if revoked in abilities:
            abilities.remove(revoked)
            self.db.execute(
                "UPDATE game_cards SET card_abilities=? WHERE session_id=? "
                "AND card_uid=?",
                (json.dumps(abilities), self.session.session_id, int(target)))
            self.db.commit()
        from .bom import _push_card_state

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
        row = self.db.execute(
            "SELECT permanent_buffs, card_state FROM game_cards "
            "WHERE session_id=? AND card_uid=?",
            (self.session.session_id, int(target))).fetchone()
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
        self.db.execute(
            "UPDATE game_cards SET card_attack_mod=0, card_defense_mod=0, "
            "card_cost_mod=0, permanent_buffs=? WHERE session_id=? "
            "AND card_uid=?",
            (json.dumps(data), self.session.session_id, int(target)))
        self.db.commit()
        from .bom import _push_card_state

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
        row = self.db.execute(
            "SELECT card_state FROM game_cards WHERE session_id=? "
            "AND card_uid=?", (self.session.session_id, target)).fetchone()
        if not row:
            return "remove from combat: card not found"
        state = int(row[0] or 0) & ~int(clear)
        self.db.execute(
            "UPDATE game_cards SET card_state=? WHERE session_id=? "
            "AND card_uid=?", (state, self.session.session_id, target))
        self.db.commit()
        from .bom import _push_card_state

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
        row = self.db.execute(
            "SELECT card_state, user_id FROM game_cards WHERE session_id=? "
            "AND card_uid=?", (self.session.session_id, target)).fetchone()
        if not row:
            return None
        state = (int(row[0] or 0) | int(add or 0)) & ~int(remove or 0)
        self.db.execute(
            "UPDATE game_cards SET card_state=? WHERE session_id=? "
            "AND card_uid=?", (state, self.session.session_id, target))
        if commit:
            self.db.commit()
        from .bom import _push_card_state

        _push_card_state(
            self.game, self.session, self.db, self.handler,
            self.player_uid, self.ai_uid, target, state, self.bstate)
        if trigger:
            from .triggers import resolve_triggers

            resolve_triggers(
                self.db, self.handler, self.game, self.session,
                self.player_uid, self.ai_uid, self.bstate, trigger, target,
                source_owner_uid=int(row[1] or 0))
        elif (int(remove or 0) & int(game_engine.ECardStates.Tapped)
              and int(row[0] or 0) & int(game_engine.ECardStates.Tapped)):
            from .triggers import resolve_triggers

            resolve_triggers(
                self.db, self.handler, self.game, self.session,
                self.player_uid, self.ai_uid, self.bstate,
                "CardReadiedEvent", target,
                source_owner_uid=int(row[1] or 0))
        return state

    def replenish_resources(self) -> str:
        """Refill one controller's current resource pool."""
        import game_engine
        from ._shared import owner_uid

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
        from .bom import _record_ability_list_target
        from .kill_troop import kill_troop

        kill_troop(
            self.game, self.session, self.db, self.handler,
            self.player_uid, self.ai_uid, int(target), self.bstate,
            cause="sacrifice")
        _record_ability_list_target(self.db, self.bstate, int(target))
        return f"sacrificed {hex(int(target))}"

    def summon_token(self) -> str:
        """Create tokens through the metadata token-operation helper."""
        from .effects.tokens import summon_token

        return summon_token(
            self.game, self.session, self.db, self.handler,
            self.player_uid, self.ai_uid, self.bstate,
            self.effect_guid, self.param)

    def conscript(self) -> str:
        """Conscript cards through the shared token/zone helper."""
        from .effects.tokens import conscript_cards

        return conscript_cards(
            self.game, self.session, self.db, self.handler,
            self.player_uid, self.ai_uid, self.bstate,
            self.effect_guid, self.param)

    def load_player_deck(self) -> str:
        """Load authored player-deck cards through the shared helper."""
        from .effects.tokens import load_player_deck

        return load_player_deck(
            self.game, self.session, self.db, self.handler,
            self.player_uid, self.ai_uid, self.bstate,
            self.effect_guid, self.param)

    def activate_triggered(self) -> str:
        """Activate a typed keyword trigger on the resolved target."""
        from .triggers import manually_trigger_abilities

        keyword = self.template_value("m_Keyword", "")
        target = self.resolved_target()
        if target is None:
            return "activate triggered: no target"
        result = manually_trigger_abilities(
            self.db, self.handler, self.game, self.session,
            self.player_uid, self.ai_uid, self.bstate,
            int(target), keyword)
        return (f"activated {keyword} on {hex(int(target))}: {result}"
                if result else f"activated {keyword}: no matching ability")

    def counter_spell(self) -> str:
        """Resolve a counter-spell operation through the trigger boundary."""
        from .triggers import _resolve_counter_spell

        return _resolve_counter_spell(
            self.db, self.handler, self.game, self.session,
            self.player_uid, self.ai_uid, self.bstate,
            self.effect_guid, self.param, "")

    def destroy(self, target: int | None = None) -> str:
        """Destroy a resolved card or champion through the normal death path."""
        target = self.resolved_target() if target is None else target
        if target is None:
            return "destroy: no target"
        from .kill_troop import kill_troop

        target = int(target)
        card_row = self.db.execute(
            "SELECT 1 FROM game_cards WHERE session_id=? AND card_uid=?",
            (self.session.session_id, target)).fetchone()
        if not card_row:
            from .bom import _controller_id_for_target

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
        from ._shared import owner_uid

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

        from ._shared import owner_uid, pvp_opponent_pid

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
        from . import bom

        return getattr(bom, function_name)(
            self.game, self.session, self.db, self.handler,
            self.player_uid, self.ai_uid, self.bstate,
            self.effect_guid, self.param)

    def _emit_authored_event(self, event_type: str,
                             target: int | None = None) -> str:
        """Dispatch one metadata-defined ability event.

        ``FireEventEffectTemplate`` is the authored bridge for keyword-like
        events such as Fateweave, Illuminate, Verdict, and Prophecy. Keep the
        dispatch here so the effect uses the same trigger, condition, and
        target machinery as ordinary game events in both PVE and PVP.
        """
        from .triggers import resolve_triggers

        source = self.bstate.get("resolving_source_uid")
        if source is None:
            source = target
        if source is None:
            return f"{event_type}: no source"
        source = int(source)
        owner = self.target_owner(source, default=None)
        if owner is None:
            owner = int(self.bstate.get("resolving_owner_id", 0) or 0)
        result = resolve_triggers(
            self.db, self.handler, self.game, self.session,
            self.player_uid, self.ai_uid, self.bstate,
            str(event_type).rsplit(".", 1)[-1], source,
            source_owner_uid=int(owner),
            extra_target=(int(target) if target is not None and
                          int(target) != source else None))
        return f"fired {str(event_type).rsplit('.', 1)[-1]}" + (
            f": {result}" if result else "")

    def create_and_cast_spell(self):
        return self._legacy("_create_and_cast_spell_legacy")

    def verdict(self):
        return self._emit_authored_event("VerdictEvent")

    def grant_ability(self):
        return self._legacy("_grant_ability_legacy")

    def copy_ability(self):
        return self._legacy("_copy_ability_legacy")

    def play_card(self):
        return self._legacy("_play_card_legacy")

    def fire_event(self):
        event_type = self.template_value("m_TriggerType", "") or ""
        event_type = str(event_type).rsplit(".", 1)[-1]
        if event_type in ("", "GenericEvent"):
            # Older records use GenericEvent for the named Prophesied
            # operation. The effect template name is the typed fallback.
            name = str(self.template_value("m_Name", "") or "")
            if name.lower().startswith("fire") and name.endswith("Event"):
                event_type = name[4:]
        if not event_type:
            return "fire event: no authored event type"
        return self._emit_authored_event(event_type, self.resolved_target())

    def activate_ability(self):
        return self._legacy("_activate_ability_legacy")

    def tac(self):
        return self._legacy("_tac_legacy")

    def block(self):
        from .effects.combat import _block_legacy

        return _block_legacy(
            self.game, self.session, self.db, self.handler,
            self.player_uid, self.ai_uid, self.bstate,
            self.effect_guid, self.param)

    def double_choice(self):
        from .effects.choices import _double_choice_legacy

        return _double_choice_legacy(
            self.game, self.session, self.db, self.handler,
            self.player_uid, self.ai_uid, self.bstate,
            self.effect_guid, self.param)
