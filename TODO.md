# TODO

## FRA / encounter champion ability audit — remaining divergences

Found while auditing every Frost Ring Arena and encounter champion against the
C# client (`HexClient`). The eight bugs confirmed during that audit were fixed
in `rules_port/`; the items below are the verified divergences that were left
in place because each needs a design decision or a larger change.

### Deliberately left (in scope for FRA/encounter champions)

- [ ] **TransformCard to a ChampionTemplate/MercenaryTemplate is a no-op.**
      `rules_port/transform_effects.py:49` resolves only `card_templates`;
      C# `TransformCardAbilityEffectTemplate.Apply` falls through
      CardTemplate -> ChampionTemplate -> MercenaryTemplate and synthesizes the
      champion's card via `ChampionTemplate.GetCardTemplate()` (keeping the
      target's name). Affects FRA **Tormented Locksmith/Elite** (`9484265f`),
      **Midnight Gatherer Elite** (`78c19418`), encounter **Wormoid Queen**
      (`609f5a87`).
- [ ] **CreateTokenCopy does not copy the source card's live state.**
      `rules_port/token_effects.py:859` inserts the base template; C#
      `CreateTokenCopies` calls `Card.CopyFrom(copyFrom, isReplica)`, copying
      damage, counters, boons, revoked abilities, uses/cooldowns, gems, etc.
      Affects FRA **Jovial Pippit/Elite** ("create two copies of it and put
      them into their deck").
- [ ] **CreateAndCastSpell resolves the copy inline and ignores locks.**
      `rules_port/effects.py:436` resolves the created spell synchronously
      instead of pushing it onto the chain, has no `CantCreateCopies` check,
      and clamps a typed amount of 0 up to 1; C#
      `CreateAndCastSpellAbilityEffectTemplate.Apply` -> `CopyCardAndPutOnChain`
      gives the copy a normal response window. Encounter
      **Periwinkle/Elite** ("when you play a card with cost 5 or greater,
      copy it").
- [ ] **Systemic event/RNG gaps around these champions.** No in-scope
      champion trigger listens to the missing events today, but a future
      champion/card will, and replays desync:
      - `VoidCardAbilityEffectTemplate` emits only `CardExitedZoneEvent`
        (`rules_port/context.py:691`); C# `Session.VoidCard` also enqueues
        `CardVoidedEvent` and the destination `CardEnteredZoneEvent`.
        `CardVoidedEvent` is never dispatched anywhere in `rules_port`.
      - `BuryCardAbilityEffectTemplate` emits `CardDiscardedEvent` where C#
        does not, and skips `CardExitedZoneEvent`/`HiddenCardEnteredZoneEvent`
        (`rules_port/context.py:584`).
      - Replay-visible rolls use stdlib `random` instead of the session RNG
        (`Session.RandomNumberGenerator`): `rules_port/destruction_effects.py`
        (destroy-by-defense), `rules_port/context.py` (shuffle_collection),
        `rules_port/token_effects.py` (conscript/fuse picks),
        `rules_port/transform_effects.py` (transform-at-random).

### Related port gaps surfaced by the same audit (not FRA-champion-specific)

- [ ] `RegisterTriggerAbilityEffectTemplate` writes
      `bstate["registered_triggers"]` but nothing reads it, so registered
      triggers never fire (C# `Session.RegisterTriggerEventHandler`).
- [ ] `GrantAbilityEffectTemplate` caches the random power in
      `bstate["random_power_template"]` for the whole battle instead of per
      activation (C# stores it on the ability instance), and the
      random-Inspire pool is not format/rarity filtered
      (`rules_port/effects.py:237`).
- [ ] `RevertPermanentModificationsAbilityEffectTemplate` non-transform path
      only clears stat mods via `db_reset_card_modifiers`; C# `Card.Revert`
      also clears damage, counters, dynamic abilities, damage shields,
      uses/cooldowns, card integer variables, and emits `CardReverted`
      (`rules_port/context.py:2926`).
- [ ] `MoveCardToZoneEffectTemplate`: `m_DestinationLocation=Bottom` stores
      position 0 (= top); `m_RandomLocation`/`m_TopHalfOfDeck` insert uniformly
      over the whole deck instead of the top N; `m_ControlGivenToTargetIndex=-2`
      and `m_UseSourceDestination` are unhandled
      (`rules_port/context.py`, `pvp_db.db_randomly_insert_deck_cards`).
- [ ] `DiscardCardAbilityEffectTemplate` only accepts `hand`/`choosing`
      sources (`rules_port/context.py:1257`); C# discards from any collection.
- [ ] `ActivateAbilityEffectTemplate` resolves the child inline instead of
      pushing it on the chain after the parent, so ordering differs when the
      parent has later effects (Rhiannon of Flame, The Nameless Knight,
      Calilac).
- [ ] `LoadPlayerDeckAbilityEffectTemplate` does not shuffle the loaded deck
      (C# `Deck.Shuffle`) and uses `resolving_owner_id` instead of the resolved
      target's controller (`rules_port/deck_effects.py`).
- [ ] `SummonTokenTroopAbilityEffectTemplate` ignores
      `m_EntersPlayExhausted`/`m_EntersPlayAttacking` on the native path, and
      filter-based multi-token picks use `random.sample` (without replacement)
      where C# allows duplicates (`rules_port/token_effects.py`).
- [ ] `CreateTokenMatchingTargetAbilityEffectTemplate` forces count to >= 1
      and skips the token/non-warzone and `CantCreateCopies` guards
      (`rules_port/token_effects.py`).
- [ ] `TransformSelfAbilityEffectTemplate` non-PlantGarden path does not copy
      the target's gems/boons/counters/damage; `TransformCardToTarget` moves
      the source to the warzone instead of keeping its zone
      (`rules_port/context.py:1418`).
- [ ] `RevealCardsAbilityEffectTemplate` broadcasts `Opponents` reveals
      publicly in PvP (`rules_port/reveal_effects.py`).
- [ ] `StoreTargetsAbilityEffectTemplate` with TAC `StoreInAbility` stores in
      the persisted battle state keyed by ability GUID; C# stores on the
      ability instance and drops it with the activation (needs an explicit
      `ForgetAllCards` today).
- [ ] `tests/tests_leaves.py::test_revert_mods` fixture `make_db()` lacks
      `game_cards.original_template_guid`, so the test errors before exercising
      the leaf.
