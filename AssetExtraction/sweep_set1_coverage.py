#!/usr/bin/env python3
"""Sweep Set 1 'Shards of Fate' card coverage against the current engine.

For every Set 1 card: resolves its ability BOM effect types and trigger
events, marks each against what the engine can actually execute, and reports
the gaps (missing leaves, stubbed leaves, unmodeled conditions, undispatched
trigger events) so the port can be driven by real card coverage.

Usage:
    python3 AssetExtraction/sweep_set1_coverage.py
"""

import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DB = os.path.join(ROOT, "hconnect.db")
SET_GUID = "0382f729-7710-432b-b761-13677982dcd2"

# Trigger events the engine actually dispatches today.
DISPATCHED_EVENTS = {
    "Game.Shared.Mechanics.CardEnteredZoneEvent",
    "Game.Shared.Mechanics.AsEntersPlayEvent",
    "Game.Shared.Mechanics.ChampionHealedEvent",
    "Game.Shared.Mechanics.CardAttackedEvent",
    "Game.Shared.Mechanics.CardDrawnEvent",
    "Game.Shared.Mechanics.CardExitedZoneEvent",
    "Game.Shared.Mechanics.TurnStartedEvent",
    "Game.Shared.Mechanics.CardCastEvent",
    "Game.Shared.Mechanics.CardDealtDamageEvent",
    "Game.Shared.Mechanics.TurnEndedEvent",
    "Game.Shared.Mechanics.CardBlockedEvent",
    "Game.Shared.Mechanics.CardSacrificedEvent",
    "Game.Shared.Mechanics.GameStartedEvent",
    "Game.Shared.Mechanics.CardAttackedOrBlockedEvent",
    "Game.Shared.Mechanics.CardWouldEnterZoneEvent",
    "Game.Shared.Mechanics.CardWouldBeDrawnEvent",
    "Game.Shared.Mechanics.CardWouldBeDamagedEvent",
    "Game.Shared.Mechanics.CardCreatedEvent",
}

# Condition types the condition_engine models (last segment).
MODELED_CONDITIONS = {
    "AndTriggerCondition", "OrTriggerCondition", "NotTriggerCondition",
    "AndEffectCondition", "OrEffectCondition", "NotEffectCondition",
    "AndAbilityCondition",
    "TriggerCardIsAbilitySource", "TriggerPlayerControlsAbilitySource",
    "TriggerPlayerControlsCard", "TriggerPlayerControlsTarget",
    "TriggerCardMatchesFilter", "TriggerCardEnteredZone",
    "TriggerCardIsNthCardDrawnThisTurnByThisPlayer",
    "TriggerPlayerIsActivePlayer", "TriggerCardSameNameInZone",
    "TriggerCardCounter", "TriggerPlayerHealth",
    "AbilityControllerHasThresholdAbilityCondition",
    "AbilityControllerIsActiveAbilityCondition",
    "AbilityControllerHasPriorityAbilityCondition",
    "SourceCardHasCounters", "RequiresCardsControlled",
    "CardFilterAbilityCondition",
    "RequiresChampionHealth", "RequiresChampionCharges",
    "RequiresResourceThreshold", "RequiresTotalResources",
    "ChampionActionsCastThisTurn",
    "TriggerCardIsStoredTargetOfAbilitySource",
    "RequiresSourcePassesFilterCondition",
}

# Card-filter types the targeting layer can evaluate (last segment).
MODELED_FILTERS = {
    "AndCardFilter", "OrCardFilter", "NotCardFilter",
    "IsType", "IsTroop", "IsArtifact", "IsResource", "IsHero",
    "IsSubType", "IsAttacking", "IsTapped", "IsAbilitySource",
    "IsCardName", "IsNotControlledBy", "HasSourceCastingCostFilter",
    "IsColor", "DamagedOpponentThisTurn", "InZone", "IsControlledBy",
    "HasAttackValue", "HasDefenseValue",
}


def collect_condition_filters(node, cond_acc, filt_acc, in_filter=False):
    if not isinstance(node, dict):
        return
    t = str(node.get("_t", "")).split(".")[-1]
    if t:
        (filt_acc if in_filter else cond_acc).add(t)
    for key in ("m_Condition", "m_Conditions"):
        if isinstance(node.get(key), dict):
            collect_condition_filters(node[key], cond_acc, filt_acc, False)
        else:
            for c in (node.get(key) or []):
                collect_condition_filters(c, cond_acc, filt_acc, False)
    for key in ("m_CardFilter", "m_Filter", "m_TargetFilter",
                "m_QuantityCardFilter"):
        if isinstance(node.get(key), dict):
            collect_condition_filters(node[key], cond_acc, filt_acc, True)
        else:
            for c in (node.get(key) or []):
                collect_condition_filters(c, cond_acc, filt_acc, True)
    for c in (node.get("m_TargetFilters") or []):
        collect_condition_filters(c, cond_acc, filt_acc, in_filter)


def condition_types(node, acc):
    """Backwards-compatible collector: every type under a condition root."""
    if not isinstance(node, dict):
        return
    t = str(node.get("_t", "")).split(".")[-1]
    if t:
        acc.add(t)
    for key in ("m_Condition", "m_Conditions", "m_CardFilter", "m_Filter",
                "m_TargetFilter", "m_QuantityCardFilter", "m_TargetFilters"):
        if isinstance(node.get(key), dict):
            condition_types(node[key], acc)
        else:
            for c in (node.get(key) or []):
                condition_types(c, acc)


def main():
    db = sqlite3.connect(DB)
    try:
        from abilities.framework.bom import _LEAFS
    except Exception:
        _LEAFS = {}
    # Trigger-path-only executors (handled inside _resolve_ability_bom).
    trigger_path = {"CardModifierAbilityEffectTemplate",
                    "SummonTokenTroopAbilityEffectTemplate",
                    "MoveCardToZoneEffectTemplate",
                    "CounterSpellAbilityEffectTemplate",
                    "TransformCardAbilityEffectTemplate",
                    "ActivateAbilityEffectTemplate"}
    # Leaves registered but effectively no-ops.
    stubs = {"DiscardCardAbilityEffectTemplate", "VerdictAbilityEffectTemplate",
             "FireEventEffectTemplate"}

    cards = db.execute(
        "SELECT guid, name, abilities_json FROM card_templates "
        "WHERE set_guid=? AND card_type!='Resource'", (SET_GUID,)).fetchall()

    effect_counts = {}
    effect_status = {}
    event_counts = {}
    green = 0
    needs_work = 0
    effect_gap_cards = {}
    event_gap_cards = {}
    cond_gap_cards = {}
    filter_gap_cards = {}

    for guid, name, ab_json in cards:
        try:
            ags = json.loads(ab_json or "[]")
        except Exception:
            ags = []
        card_ok = True
        for ag in ags:
            rows = db.execute(
                "SELECT effect_type, param FROM ability_effects WHERE ability_guid=?",
                (ag,)).fetchall()
            if not rows:
                # Ability with no BOM rows (e.g. pure text/passive).
                continue
            for etype, param in rows:
                effect_counts[etype] = effect_counts.get(etype, 0) + 1
                if etype in stubs:
                    status = "stub"
                elif etype in trigger_path:
                    status = "ok"
                elif etype in _LEAFS:
                    status = "ok"
                else:
                    status = "missing"
                effect_status.setdefault(etype, status)
                if status != "ok":
                    card_ok = False
                    effect_gap_cards.setdefault(etype, set()).add(name)
            mrow = db.execute(
                "SELECT trigger_event_type, raw_json FROM card_abilities_meta "
                "WHERE ability_guid=?", (ag,)).fetchone()
            if mrow and mrow[0]:
                event_counts[mrow[0]] = event_counts.get(mrow[0], 0) + 1
                if mrow[0] not in DISPATCHED_EVENTS:
                    card_ok = False
                    event_gap_cards.setdefault(mrow[0], set()).add(name)
            if mrow and mrow[1]:
                try:
                    rec = json.loads(mrow[1])
                except Exception:
                    rec = {}
                cond_acc = set()
                filt_acc = set()
                for root in (rec.get("m_TriggerCondition"),
                             rec.get("m_AbilityCondition")):
                    collect_condition_filters(root, cond_acc, filt_acc)
                unmodeled = cond_acc - MODELED_CONDITIONS
                if unmodeled:
                    card_ok = False
                    cond_gap_cards.setdefault(",".join(sorted(unmodeled)),
                                              set()).add(name)
                unmodeled_f = filt_acc - MODELED_FILTERS
                if unmodeled_f:
                    card_ok = False
                    filter_gap_cards.setdefault(",".join(sorted(unmodeled_f)),
                                                set()).add(name)
        if card_ok:
            green += 1
        else:
            needs_work += 1

    print(f"Set 1 cards (non-resource): {len(cards)}")
    print(f"  fully covered: {green}   needs work: {needs_work}")
    print()
    print("=== Effect-type gaps (count | status | cards) ===")
    for etype in sorted(effect_counts, key=lambda e: -effect_counts[e]):
        status = effect_status[etype]
        if status != "ok":
            names = sorted(effect_gap_cards.get(etype, set()))
            print(f"  {status:8s} {effect_counts[etype]:4d}  {etype}  "
                  f"[{', '.join(names[:5])}{'...' if len(names) > 5 else ''}]")
    print()
    print("=== Trigger events ===")
    for ev in sorted(event_counts, key=lambda e: -event_counts[e]):
        dispatched = "OK" if ev in DISPATCHED_EVENTS else "NOT DISPATCHED"
        names = sorted(event_gap_cards.get(ev, set()))
        print(f"  {dispatched:14s} {event_counts[ev]:4d}  {ev.split('.')[-1]}  "
              f"[{', '.join(names[:4])}{'...' if len(names) > 4 else ''}]")
    print()
    print("=== Unmodeled condition types ===")
    for conds, names in sorted(cond_gap_cards.items(), key=lambda kv: -len(kv[1])):
        print(f"  {len(names):4d} cards  {conds}  [{', '.join(sorted(names)[:4])}"
              f"{'...' if len(names) > 4 else ''}]")
    print()
    print("=== Unmodeled card filter types ===")
    for conds, names in sorted(filter_gap_cards.items(),
                               key=lambda kv: -len(kv[1])):
        print(f"  {len(names):4d} cards  {conds}  [{', '.join(sorted(names)[:4])}"
              f"{'...' if len(names) > 4 else ''}]")


if __name__ == "__main__":
    main()
