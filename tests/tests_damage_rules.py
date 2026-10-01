"""Shared C# damage ordering and accounting, with isolated SQLite projection."""
import json
import os
import sqlite3
import sys
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.test_db import fresh_database
fresh_database()
import game_engine
from rules_port.damage_effects import (
    DamageOutcome, deal_damage, card_damage_multiplier, _additive_adjustment)
from rules_port.combat_damage import resolve
from rules_port import static_rules


class DamageRules(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE game_cards (session_id INTEGER, card_uid INTEGER, '
                        'user_id INTEGER, template_guid TEXT, card_type TEXT, location TEXT, '
                        'permanent_buffs TEXT, temporary_buffs TEXT, card_damage INTEGER)')
        for uid, owner in ((769, 5), (1025, 0), (1281, 0)):
            self.db.execute('INSERT INTO game_cards VALUES (1, ?, ?, ?, ?, ?, ?, ?, 0)',
                            (uid, owner, 'fixture', 'Troop', 'warzone', '{}', '{}'))
        self.state = {'resolving_source_uid': 769, 'player_health': 20, 'ai_health': 20}
        self.events = []
        self.health = {769: 20, 1025: 20, 1281: 20}
        self.attrs = {769: int(game_engine.ECardAttributes.SpiritDrain)}
        self.flags = {}
        self.handler = SimpleNamespace(user_profile={'id': 5},
            _player_champ_scid=SimpleNamespace(uid=257),
            _ai_champ_scid=SimpleNamespace(uid=513))
        self.ctx = SimpleNamespace(db=self.db, session=SimpleNamespace(session_id=1),
            handler=self.handler, bstate=self.state,
            player_uid=game_engine.UID.make(244, 5), ai_uid=game_engine.UID.make(3, 1000),
            game=SimpleNamespace(
                _push=lambda event: self.events.append(('wire', event)),
                push_begin_combat_resolution=lambda: self.events.append(
                    ('wire', 'begin-combat')),
                push_combat_phase_resolved=lambda *args, **kwargs: self.events.append(
                    ('wire', 'combat-phase')),
                push_end_combat_resolution=lambda: self.events.append(
                    ('wire', 'end-combat'))),
            target_owner=lambda uid, default=None: {257: 5, 513: 0, 769: 5,
                                                   1025: 0, 1281: 0}.get(uid, default),
            _emit_trigger=self.trigger,
            update_card_state=lambda uid, **kw: self.events.append(('update', uid)),
            _push_champion_intattrs=lambda owner, uid: None,
            _push_modifier_card=lambda uid, **kw: None,
            _champion_owner=lambda uid: None,
            gain_health=lambda owner, amount: self.events.append(('heal', owner, amount)),
            destroy=lambda uid: self.events.append(('destroy', uid)))
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('rules_port.static_rules.effective_stats', side_effect=self.stats))
        self.stack.enter_context(patch('rules_port.static_rules._native_static_deltas',
                                      return_value=(static_rules._empty_deltas(), False)))

    def stats(self, db, session, state, uid):
        row = db.execute('SELECT card_damage FROM game_cards WHERE card_uid=?', (uid,)).fetchone()
        return (5, self.health.get(uid, 20) - (row[0] if row else 0),
                self.attrs.get(uid, 0), set(self.flags.get(uid, ())), 0)

    def trigger(self, name, source, owner=None, **kw):
        self.events.append((name, source, kw))
        return False

    def buffs(self, uid, data, column='permanent_buffs'):
        self.db.execute('UPDATE game_cards SET ' + column + '=? WHERE card_uid=?',
                        (json.dumps(data), uid))

    def test_multiplier_shield_replacement_mutation_and_lifedrain_order(self):
        self.buffs(769, {'rule_modifiers': [{'property': 'damagemultiplier', 'value': 2}]})
        self.buffs(1025, {'damage_shields': [{'amount': 3}]})
        result = DamageOutcome()
        deal_damage(self.ctx, 1025, 4, outcome=result)
        self.assertEqual((result.dealt, result.absorbed), (5, 8))
        self.assertEqual(self.db.execute('SELECT card_damage FROM game_cards WHERE card_uid=1025').fetchone()[0], 5)
        self.assertEqual([event[0] for event in self.events], [
            'DamagePreventedEvent', 'CardWouldDealDamageEvent', 'CardWouldBeDamagedEvent',
            'heal', 'update', 'CardDealtDamageEvent', 'CardDamagedEvent'])
        self.assertEqual(self.events[0][2]['event_tac']['DamagePrevented'], 3)
        self.assertEqual(self.events[1][2]['event_tac']['damage'], 5)
        self.assertEqual(self.events[3], ('heal', 5, 5))

    def test_immunity_preserves_shields_and_replacement_sees_reduced_damage(self):
        self.buffs(1025, {'damage_shields': [{'amount': 3}]})
        with patch('rules_port.damage_effects._damage_immune', return_value=True):
            result = DamageOutcome()
            deal_damage(self.ctx, 1025, 4, outcome=result)
        self.assertEqual((result.dealt, result.absorbed), (0, 4))
        self.assertEqual(self.events, [])
        shields = json.loads(self.db.execute('SELECT permanent_buffs FROM game_cards WHERE card_uid=1025').fetchone()[0])
        self.assertEqual(shields['damage_shields'][0]['amount'], 3)
        def replacement(name, *args, **kwargs):
            self.trigger(name, *args, **kwargs)
            return name == 'CardWouldDealDamageEvent'
        self.ctx._emit_trigger = replacement
        self.assertEqual(deal_damage(self.ctx, 1025, 4), 'replaced')
        self.assertEqual(self.events[-1][2]['event_tac']['damage'], 1)
        self.assertFalse(any(e[0] == 'heal' for e in self.events))

    def test_champion_shields_rehydrate_restrict_and_expire_in_both_modes(self):
        from rules_port.lifecycle import complete_turn
        from rules_port.pvp_lifecycle import advance_turn_state
        for pvp in (False, True):
            self.state.update(pvp=pvp, pids=[5, 9], turn_pid=5, turn_player='player',
                              champ_map={'5': 257, '9': 513})
            self.ctx.target_owner = lambda uid, default=None: {
                257: 5, 513: 9 if pvp else 0, 769: 5,
                1025: 9 if pvp else 0, 1281: 9 if pvp else 0}.get(uid, default)
            self.state['damage_shields'] = {'513': [
                {'amount': 3, 'only_combat': True, 'dealer': 769},
                {'amount': 9, 'one_shot': True, 'lasts_indefinitely': True}]}
            # Serialized checkpoint is the reconnect boundary for champions.
            self.ctx.bstate = json.loads(json.dumps(self.state))
            self.ctx.bstate['combat_damage'] = False
            deal_damage(self.ctx, 513, 2)
            self.assertEqual(len(self.ctx.bstate['damage_shields']['513']), 1)
            self.assertEqual(self.ctx.bstate['damage_shields']['513'][0]['amount'], 3)
            self.ctx.bstate.update(combat_damage=True, resolving_source_uid=1281)
            deal_damage(self.ctx, 513, 2)
            self.assertEqual(self.ctx.bstate['damage_shields']['513'][0]['amount'], 3)
            self.assertEqual(self.ctx.bstate['hp_9' if pvp else 'ai_health'], 18)
            self.ctx.bstate['resolving_source_uid'] = 769
            deal_damage(self.ctx, 513, 2)
            self.assertEqual(self.ctx.bstate['damage_shields']['513'][0]['amount'], 1)
            self.ctx.bstate['damage_shields']['513'].append(
                {'amount': 7, 'lasts_indefinitely': True})
            if pvp:
                advance_turn_state(self.ctx.bstate, [5, 9])
            else:
                complete_turn(self.ctx.bstate)
            self.assertEqual(self.ctx.bstate['damage_shields']['513'],
                             [{'amount': 7, 'lasts_indefinitely': True}])

    def test_numeric_multipliers_stack_replace_and_preserve_zero(self):
        rules = [{'property': 'damagemultiplier', 'value': 2},
                 {'property': 'damagemultiplier', 'value': 3},
                 {'property': 'damagemultiplier', 'value': 4, 'combatdamageonly': True}]
        self.buffs(769, {'rule_modifiers': rules})
        self.assertEqual(card_damage_multiplier(self.db, 1, self.state, 769, False), 6)
        self.assertEqual(card_damage_multiplier(self.db, 1, self.state, 769, True), 24)
        rules.append({'property': 'damagemultiplier', 'value': 5, 'replaceexistingvalue': True})
        self.buffs(769, {'rule_modifiers': rules})
        self.assertEqual(card_damage_multiplier(self.db, 1, self.state, 769, True), 20)
        rules.append({'property': 'damagemultiplier', 'value': 0})
        self.buffs(769, {'rule_modifiers': rules})
        self.assertEqual(card_damage_multiplier(self.db, 1, self.state, 769, True), 0)
        deal_damage(self.ctx, 1025, 4)
        self.assertFalse(any(e[0] == 'heal' for e in self.events))
        self.db.execute("UPDATE game_cards SET card_type='Artifact' WHERE card_uid=1025")
        self.assertEqual(deal_damage(self.ctx, 1025, 4), 'damage: target cannot take damage')

    def test_combat_prevents_before_minimum_and_consumes_assignment(self):
        self.health[1025] = 2
        self.buffs(1025, {'damage_shields': [{'amount': 3}]})
        self.state.update(combat_damage=True)
        outcome = DamageOutcome()
        deal_damage(self.ctx, 1025, 6, outcome=outcome, only_minimum=True)
        self.assertEqual((outcome.dealt, outcome.absorbed), (2, 5))
        self.assertIn(('heal', 5, 2), self.events)
        # Integration through CombatResolver: fully shielded first blocker
        # consumes all damage, so a second blocker receives no overflow.
        self.db.execute('UPDATE game_cards SET card_damage=0')
        self.buffs(1025, {'damage_shields': [{'amount': 9}]})
        self.state.update(player_attackers={'769': 513}, ai_blockers={'769': [1025, 1281]})
        self.attrs[1025] = self.attrs[1281] = int(game_engine.ECardAttributes.FirstStrike)
        with patch('rules_port.death_effects.state_based_deaths'):
            resolve(self.ctx)
        self.assertEqual(self.db.execute('SELECT SUM(card_damage) FROM game_cards').fetchone()[0], 0)

    def test_combat_damage_publishes_the_resolution_envelope(self):
        """The client only presents combat damage inside Begin/EndCombatResolution.

        OnCombatPhaseResolved refuses to run without a preceding Begin, and the
        champion's health animation is deferred into that combat group, so an
        unwrapped native damage step left the champion HUD unchanged.
        """
        self.health[1025] = 2
        self.state.update(player_attackers={'769': 513}, ai_blockers={})
        with patch('rules_port.death_effects.state_based_deaths'):
            resolve(self.ctx)
        wire = [event[1] for event in self.events if event[0] == 'wire']
        self.assertEqual(wire[0], 'begin-combat')
        self.assertEqual(wire[1], 'combat-phase')
        self.assertEqual(wire[-1], 'end-combat')

    def test_armor_absorbs_before_replacement_and_resets_at_ready(self):
        from rules_port.lifecycle import reset_armor
        self.buffs(1025, {'int_attrs': {'Armor': 3}})
        outcome = DamageOutcome()
        deal_damage(self.ctx, 1025, 5, outcome=outcome)
        self.assertEqual((outcome.dealt, outcome.absorbed), (2, 5))
        self.assertEqual(self.db.execute(
            'SELECT card_damage FROM game_cards WHERE card_uid=1025'
        ).fetchone()[0], 2)
        stored = json.loads(self.db.execute(
            'SELECT permanent_buffs FROM game_cards WHERE card_uid=1025'
        ).fetchone()[0])
        self.assertEqual(stored['int_attrs']['ArmorUsed'], 3)
        self.assertEqual([
            event[2]['event_tac']['DamagePrevented'] for event in self.events
            if event[0] == 'DamagePreventedEvent'], [3])
        # Spent armor no longer prevents damage for the rest of the turn.
        outcome = DamageOutcome()
        deal_damage(self.ctx, 1025, 1, outcome=outcome)
        self.assertEqual(outcome.dealt, 1)
        # The Ready-state reset restores the full pool for the next hit.
        reset_armor(self.db, 1, self.state)
        stored = json.loads(self.db.execute(
            'SELECT permanent_buffs FROM game_cards WHERE card_uid=1025'
        ).fetchone()[0])
        self.assertNotIn('ArmorUsed', stored.get('int_attrs', {}))
        outcome = DamageOutcome()
        deal_damage(self.ctx, 1025, 2, outcome=outcome)
        self.assertEqual((outcome.dealt, outcome.absorbed), (0, 2))
        self.assertEqual(self.db.execute(
            'SELECT card_damage FROM game_cards WHERE card_uid=1025'
        ).fetchone()[0], 3)

    def test_chance_prevention_rolls_when_the_attr_is_present(self):
        class Rng:
            def __init__(self, value):
                self.value = value
                self.calls = 0

            def next(self, low, high):
                self.calls += 1
                return self.value

        self.buffs(1025, {'int_attrs': {
            'ChanceToPreventNonCombatDamage': 50}})
        rng = Rng(10)
        self.state['_rules_rng'] = rng
        outcome = DamageOutcome()
        self.assertEqual(deal_damage(self.ctx, 1025, 4, outcome=outcome),
                         'damage: chance prevented')
        self.assertEqual(outcome.absorbed, 4)
        self.assertEqual(outcome.dealt, 0)
        self.assertEqual(rng.calls, 1)
        self.assertEqual([
            event[2]['event_tac']['DamagePrevented'] for event in self.events
            if event[0] == 'DamagePreventedEvent'], [4])
        rng.value = 90
        self.events.clear()
        self.assertEqual(deal_damage(self.ctx, 1025, 4), 'survives')
        self.assertEqual(rng.calls, 2)
        # A stored zero still consumes the roll (C# reads >= 0).
        self.buffs(1025, {'int_attrs': {
            'ChanceToPreventNonCombatDamage': 0}})
        rng.value = 0
        self.assertEqual(deal_damage(self.ctx, 1025, 4), 'survives')
        self.assertEqual(rng.calls, 3)

    def test_additive_received_modifier_and_source_prevention(self):
        self.buffs(1025, {'int_attrs': {'DamageReceivedModifier': -1}})
        outcome = DamageOutcome()
        deal_damage(self.ctx, 1025, 3, outcome=outcome)
        self.assertEqual(outcome.dealt, 2)
        self.flags[769] = {'prevent_my_noncombat_damage'}
        self.assertEqual(deal_damage(self.ctx, 1025, 3),
                         'damage: source prevention')
        self.flags.clear()

    def test_authored_champion_damage_modifiers_project_through_metadata(self):
        """Blackheart's authored champion targets affect both sides of damage."""
        source = sqlite3.connect(fresh_database())
        db = sqlite3.connect(':memory:')
        self.addCleanup(source.close)
        self.addCleanup(db.close)
        source.backup(db)
        blackheart = 'df818ff9-db9e-4ddf-ad10-8ef988421e22'
        abilities = [
            '3861715b-de57-0545-e273-c4b9926a6d85',
            'd70ad7c7-1dea-e644-7ed5-4fb4dbadbaa8',
        ]
        db.execute(
            'INSERT INTO game_cards '
            '(user_id, session_id, card_uid, card_template_id, location, '
            'card_type, template_guid, card_abilities, owner_user_id) '
            'VALUES (?,?,?,?,?,?,?,?,?)',
            (5, 1, 769, 0, 'warzone', 'Troop', blackheart,
             json.dumps(abilities), 5))
        db.commit()
        state = {
            'pvp': True,
            'pids': [5, 0],
            'champ_map': {'5': 257, '0': 513},
            'resolving_source_uid': 769,
        }
        own = static_rules.player_int_attributes(
            db, 1, state, 5, target_uid=257, include_runtime=False)
        opposing = static_rules.player_int_attributes(
            db, 1, state, 0, target_uid=513, include_runtime=False)
        self.assertEqual(own.get('DamageReceivedModifier'), -1)
        self.assertEqual(opposing.get('DamageReceivedModifier'), 1)

        context = SimpleNamespace(
            db=db, session=SimpleNamespace(session_id=1), bstate=state,
            target_owner=lambda uid, default=None: {
                257: 5, 513: 0, 769: 5}.get(uid, default))
        self.assertEqual(_additive_adjustment(context, 257, 769, True, True), -1)
        self.assertEqual(_additive_adjustment(context, 513, 769, True, True), 1)
        self.assertEqual(_additive_adjustment(context, 257, None, False, True), -1)
        self.assertEqual(_additive_adjustment(context, 513, None, False, True), 1)

    def test_continuous_rules_respect_authored_targets(self):
        rule = {'property': 'damagemultiplier', 'amount': 3}
        with patch.object(static_rules, '_card_location_row', return_value=(5, 'warzone', 0)), \
             patch.object(static_rules, '_owner_static_sources', return_value=[769]), \
             patch.object(static_rules, '_static_abilities', return_value=['fixture']), \
             patch.object(static_rules, '_static_leaves', return_value=[(rule, '{}')]), \
             patch.object(static_rules, '_static_condition_matches', return_value=True), \
             patch.object(static_rules, '_target_matches', return_value=False) as target:
            deltas, unsupported = static_rules._scan_static_deltas(self.db, 1, {}, 1025, static_rules._ProjectionCache())
            self.assertEqual(deltas['rules'], [])
            target.return_value = True
            deltas, unsupported = static_rules._scan_static_deltas(self.db, 1, {}, 1025, static_rules._ProjectionCache())
            self.assertEqual(deltas['rules'][0]['value'], 3)


if __name__ == '__main__':
    unittest.main()
