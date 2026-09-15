"""Auditable inventory of the native HexClient rules conversion."""

from pathlib import Path

TRANSACTION_COVERAGE = {
    "AcceptStartingHandTransaction": "native",
    "ActivateAbilityTransaction": "native+projection",
    "ActivateTriggeredAbiliesTransaction": "native+projection",
    # These transactions have native rule decisions. Their projection is the
    # host-owned SQLite/wire boundary, not a second legacy rules engine.
    "AssignDamageOrderTransaction": "native+projection",
    "CancelAutoPassTransaction": "native",
    "ChooseDrawTransaction": "native",
    "ChoosePlayTransaction": "native",
    "CommitTroopsToAttackTransaction": "native+projection",
    "CommitTroopsToDefenseTransaction": "native+projection",
    "DiscardTransaction": "native+projection",
    "EncounterModDialogTransaction": "native+projection",
    "MulliganTransaction": "native+projection",
    "PassPriorityTransaction": "native",
    "PlayArtifactTransaction": "native+projection",
    "PlayChampionTransaction": "native+projection",
    "PlayResourceTransaction": "native+projection",
    "PlaySpellTransaction": "native+projection",
    "PlayTroopTransaction": "native+projection",
    "QuitGameTransaction": "native",
    "ReadyCardTransaction": "native+projection",
    "RequestPlayerOptionsTransaction": "native+projection",
    "RequestPrioritySyncTransaction": "native",
    "SendGameStateChecksumTransaction": "native",
    "SetAbilityActivationDataTransaction": "native",
    "SetAutoPassTransaction": "native",
    "SetTurnPhasesTransaction": "native",
    "TipWindowClosed": "native",
    "DebugCheatTransaction": "legacy-only",
    "NonsenseTransaction": "legacy-only",
}

FILTER_COVERAGE = {
    # Abstract/static wrappers and predicates already represented by the
    # composable Python filter layer.
    "CardFilter": "structural", "IsAlternateArt": "native",
    "IsExtendedArt": "native", "IsPromo": "native",
    "StaticCardFilter": "structural", "StaticAndCardFilter": "native",
    "StaticOrCardFilter": "native", "StaticNotCardFilter": "native",
    "AnyCard": "native", "IsBasic": "native", "IsUniqueCard": "native",
    "IsCardName": "native", "NameContainsFilter": "native",
    "HasAttackedThisTurn": "native",
    "CompareAttackAndDefenseFilter": "native",
    "CompareAttackToHighestFilter": "native",
    "CompareAttackToLowestFilter": "native",
    "CompareDefenseToLowestFilter": "native",
    "CompareHealthToHighestFilter": "native",
    "CompareHealthToLowestFilter": "native",
    "CompareResourceCostToHighestFilter": "native",
    "CompareResourceCostToMyHighestFilter": "native",
    "HasSourceTypeFilter": "native", "HasSourceResourceCost": "native",
    "DifferentOwners": "native",
    "HasASharedFactionWithSourceFilter": "native",
    "HasASharedRarityWithSourceFilter": "native",
    "HasASharedSubtypeWithSourceFilter": "native",
    "HasASharedClassWithSourceChampionFilter": "native",
    "HasASharedShardWithSourceFilter": "native",
    "HasASharedShardWithTopOfChainFilter": "native",
    "HasASharedSubtypeWithSourceChampionFilter": "native",
    "MovedBySource": "native", "IsChildOfAbilitySource": "native",
    "IsParentOfAbilitySource": "native", "IsParentOfAbilitySourceFilter": "native",
    "InCombatWithSourceFilter": "native", "DamagedOpponentThisTurn": "native",
    "HasSourceCastingCostFilter": "native",
    "CompareCastingCostToSourceCountersFilter": "native",
    "HasCountersValue": "native",
    "IntAttrFilter": "native", "StringAttrFilter": "native",
    "SetIdFilter": "native", "SetNumberFilter": "native",
    "MatchesTargetFilter": "native", "TopNOfDeck": "native",
    "TACFilter": "native",
    "PlayerMeetsThresholdRequirementsToCast": "native",
    "PlayersWhoControlMatchingFilter": "native",
    "BlockingFilter": "native", "BeingBlockedByFilter": "native",
    "OtherTroops": "native", "IsAbilitySource": "native",
    "IsTopCard": "native",
    "IsTranformed": "native", "IsPvECard": "native",
    "IsMercenaryFilter": "native", "IsEquippedCardFilter": "native",
    "IsStoredCardFilter": "native", "IsColor_DeckBuilder": "native",
    "InZone": "native", "InCollection": "native", "IsTapped": "native",
    "IsType": "native", "IsNotType": "native", "IsControlledBy": "native",
    "IsNotControlledBy": "native", "HasAnyAttributeFlags": "native",
    "HasAllAttributeFlags": "native",
    "HasCastingCost": "native", "HasResourceCost": "native",
    "HasAttackValue": "native", "HasDefenseValue": "native",
    "HasKeywordAbility": "native", "HasName": "native", "IsRarity": "native",
    "IsToken": "native", "IsResource": "native", "IsQuick": "native",
    "IsTroop": "native", "IsArtifact": "native",
    "IsHero": "native", "IsDamaged": "native", "IsAttacking": "native",
    "IsBlocking": "native", "IsPlayedThisTurn": "native",
    "IsDamagedThisTurn": "native", "IsHealedThisTurn": "native",
    "IsSocketable": "native", "IsSocketed": "native", "HasTag": "native",
    "IsSubType": "native", "IsMultiThresholdCard": "native",
    "IsColor": "native", "InFaction": "native", "AndCardFilter": "native",
    "OrCardFilter": "native", "NotCardFilter": "native",
}

# These are client types whose Apply path is owned by the resolver itself or
# which are abstract bases and therefore do not need a leaf registration.
STRUCTURAL_EFFECTS = {
    "BattleAbilityEffectTemplate", "CardAbilityEffectTemplate",
    "RepeatingAbilityEffectTemplate", "SetAbilityVariableEffectEffectTemplate",
    "SetConstantValueVariableEffectTemplate",
}

# Concrete effect templates that execute without entering the compatibility
# leaf registry when the RulesPort ability lifecycle is active.  Keep this
# list explicit so migration progress is auditable; ``_LEAFS`` alone cannot
# distinguish a native implementation from a legacy adapter.
NATIVE_EFFECTS = {
    "NoOpEffectTemplate", "DrawCardAbilityEffectTemplate",
    "ClearStoredAbilityEffectTemplate",
    "SetResponsiblePlayerAbilityEffectTemplate",
    "CopyAbilityVariableEffectTemplate", "SetCardCountVariableEffectTemplate",
    "SetCardIntegerVariableEffectTemplate",
    "SetConstantValueVariableEffectTemplate",
    "ReplenishResourcesAbilityEffectTemplate",
    "UntapCardAbilityEffectTemplate", "TapCardAbilityEffectTemplate",
    "RemoveCardFromCombatAbilityEffectTemplate",
    "SwapHealthAbilityEffectTemplate", "LoseThresholdAbilityEffectTemplate",
    "ExtraCombatsThisTurnAbilityEffectTemplate", "AnimationTriggerEffectTemplate",
    "DrawNCardsAbilityEffectTemplate",
    "PutTopOfDeckIntoHandAbilityEffectTemplate",
    "BuryCardAbilityEffectTemplate", "VoidCardAbilityEffectTemplate",
    "DiscardCardAbilityEffectTemplate", "TunnelAbilityEffectTemplate",
    "TargetPlayerTakesControlEffectTemplate", "StealCardAbilityEffectTemplate",
    "StealEffectsAbilityEffectTemplate", "LoseGameAbilityEffectTemplate",
    "RevertTransformedCardAbilityEffectTemplate",
    "StoreTargetsAbilityEffectTemplate", "StoreNameAbilityEffectTemplate",
    "StoreListAttrAbilityEffectTemplate", "RememberKeywordPowersEffectTemplate",
    "RegisterTriggerAbilityEffectTemplate", "RevokeAbilityEffectTemplate",
    "RevertPermanentModificationsAbilityEffectTemplate",
    "GiveBonusTurnAbilityEffectTemplate", "SacrificeCardAbilityEffectTemplate",
    "TransformSelfAbilityEffectTemplate",
    "TransformCardToTargetAbilityEffectTemplate",
    "TransformCardIntoReplicaAbilityEffectTemplate",
    "TransformCardAtRandomAbilityEffectTemplate",
    "TransformCardAbilityEffectTemplate",
    "CreateTokenCopyAbilityEffectTemplate",
    "CopyAbilityEffectTemplate",
    "CreateTokenMatchingTargetAbilityEffectTemplate",
    "SummonTokenTroopAbilityEffectTemplate", "SummonXTokenTroopsAbilityEffectTemplate",
    "ConscriptAbilityEffectTemplate", "LoadPlayerDeckAbilityEffectTemplate",
    "DestroyCardByDefenseAbilityEffectTemplate",
    "DiscardOrSacrificeCardAbilityEffectTemplate", "PlayerAttributeAbilityEffectTemplate",
    "ExchangeCardsAbilityEffectTemplate", "MergeCardCollectionsAbilityEffectTemplate",
    "ZombiePlagueAbilityEffectTemplate", "XarloxAbilityEffectTemplate",
    "PlanCAbilityEffectTemplate", "ShuffleCardCollectionAbilityEffectTemplate",
    "RevealCardsAbilityEffectTemplate", "Battle2CardsAbilityEffectTemplate",
    "CounterSpellAbilityEffectTemplate", "InterruptSpellAbilityEffectTemplate",
    "DestroyCardAbilityEffectTemplate", "FireEventEffectTemplate",
    "TACAbilityEffectTemplate", "VerdictAbilityEffectTemplate",
    "RandomizeVariableEffectTemplate", "RandomizeVariableAbilityEffectTemplate",
    "ConversationAbilityEffectTemplate", "ActivateTriggeredAbilityEffectTemplate",
    "ActivateAbilityEffectTemplate",
    "ActivatePowerAbilityEffectTemplate",
    "CardModifierAbilityEffectTemplate",
    "PlayCardAbilityEffectTemplate",
    "BuiltInPlayCardAbilityEffectTemplate",
    "GrantAbilityEffectTemplate",
    "CreateAndCastSpellAbilityEffectTemplate",
    "DoubleChoiceAbilityEffectTemplate",
    "ReturnToHandAbilityEffectTemplate",
    "MoveCardToZoneEffectTemplate",
    "FinishMovingCardToWarzoneEffectTemplate",
    "FinishResolvingCardAbilityEffectTemplate",
    "BlockEffectTemplate",
}

# Reserved for an effect family whose native implementation is intentionally
# limited to named metadata subsets. The current Records CardModifier set is
# fully handled and is therefore listed in NATIVE_EFFECTS instead.
STAGED_EFFECTS = set()

NATIVE_EFFECT_SUBSETS = {
}

def coverage_summary():
    counts = {}
    for status in TRANSACTION_COVERAGE.values():
        counts[status] = counts.get(status, 0) + 1
    return counts

def coverage_report():
    """Return stable counts and extraction drift for CI/reporting."""
    missing_tx, extra_tx = validate_transaction_coverage()
    missing_filters, extra_filters = validate_filter_coverage()
    missing_effects, extra_effects = validate_effect_coverage()
    return {
        "transactions": {"counts": coverage_summary(),
                         "missing": list(missing_tx), "extra": list(extra_tx)},
        "filters": {"native": sum(status == "native" for status in FILTER_COVERAGE.values()),
                    "missing": list(missing_filters), "extra": list(extra_filters)},
        "effects": {"missing": list(missing_effects),
                    "extra": list(extra_effects),
                    "native": len(NATIVE_EFFECTS),
                    "native_subsets": len(NATIVE_EFFECT_SUBSETS),
                    "staged": len(STAGED_EFFECTS),
                    "legacy_backend": len(STAGED_EFFECTS)},
    }


def validate_effect_coverage(root=None):
    """Return concrete C# effect templates not represented by Python.

    Resolver-owned structural templates are excluded explicitly; this keeps
    the result useful as a migration gate instead of requiring dummy leaves
    for abstract client classes.
    """
    base = Path(root or "HexClient")
    project = base / "Assembly-CSharp-firstpass/Assembly-CSharp-firstpass.csproj"
    if not project.is_file():
        return (), ()
    import re
    actual = set(re.findall(
        r'Include="Game\\Shared\\Mechanics\\Abilities\\([^"/]+EffectTemplate)\.cs"',
        project.read_text(encoding="utf-8-sig")))
    # Compare the client inventory with the explicit native inventory.  The
    # historical BOM registry is intentionally not evidence of a ported leaf:
    # using it here would allow a legacy-only implementation to make the
    # migration report look complete.
    handled = set(NATIVE_EFFECTS) | {
        str(name).split(":", 1)[0] for name in NATIVE_EFFECT_SUBSETS
    } | STRUCTURAL_EFFECTS
    return tuple(sorted(actual - handled)), tuple(sorted(STRUCTURAL_EFFECTS - actual))

def validate_transaction_coverage(root=None):
    """Return (missing, extra) transaction class names for an extraction tree."""
    base = Path(root or "HexClient") / "Assembly-CSharp-firstpass/Game/Shared/Mechanics/Transactions"
    if not base.is_dir():
        return (), ()
    actual = {path.stem for path in base.glob("*.cs") if path.stem != "Transaction"}
    listed = set(TRANSACTION_COVERAGE)
    return tuple(sorted(actual - listed)), tuple(sorted(listed - actual))

def validate_filter_coverage(root=None):
    base = Path(root or "HexClient") / "Assembly-CSharp-firstpass/Game/Shared/Mechanics/Cards/Filters"
    if not base.is_dir():
        return (), ()
    actual = {path.stem for path in base.glob("*.cs")}
    listed = set(FILTER_COVERAGE)
    return tuple(sorted(actual - listed)), tuple(sorted(listed - actual))
