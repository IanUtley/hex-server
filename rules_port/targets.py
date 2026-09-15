"""Target-instance bridge to the shared Records-derived target evaluator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable


@dataclass(frozen=True)
class TargetSelection:
    """C# ``TargetInstance``-equivalent input: template plus selected card IDs."""

    template_id: str
    card_ids: tuple[int, ...]

    @classmethod
    def from_values(cls, template_id, card_ids: Iterable[int] | int | None):
        values = () if card_ids is None else (
            card_ids if isinstance(card_ids, (tuple, list, set)) else (card_ids,))
        return cls(str(template_id), tuple(int(value) for value in values))


class MetadataTargetAdapter:
    """Use the existing target predicate for options and untrusted selections."""

    def __init__(self, db, session_id: int, controller_uid: int, source_uid: int,
                 *, candidates: Callable | None = None,
                 validator: Callable | None = None) -> None:
        self.db = db
        self.session_id = int(session_id)
        self.controller_uid = int(controller_uid)
        self.source_uid = int(source_uid)
        if candidates is None or validator is None:
            from .targeting import (legal_targets_for,
                                    validate_target_selection)
            candidates = candidates or legal_targets_for
            validator = validator or validate_target_selection
        self.candidates_resolver = candidates
        self.validator = validator

    def candidates(self, target, *, champions=None, battle_state=None) -> tuple[int, ...]:
        return tuple(int(value) for value in self.candidates_resolver(
            self.db, self.session_id, self.controller_uid, target, self.source_uid,
            champions=champions, battle_state=battle_state))

    def validate(self, selection: TargetSelection, *, both_players=False,
                 champions=None, battle_state=None) -> tuple[int, ...]:
        values = self.validator(
            self.db, self.session_id, self.controller_uid, selection.template_id,
            self.source_uid, selection.card_ids, both_players=both_players,
            champions=champions, battle_state=battle_state)
        return tuple(int(value) for value in values)


class FilteredTargetAdapter:
    """Evaluate a C#-shaped ``CardFilter`` over runtime card candidates."""

    def __init__(self, card_filter, cards: Callable, *, context: dict | None = None):
        self.card_filter = card_filter
        self.cards = cards
        self.context = dict(context or {})

    def candidates(self, *, source=None, player=None, session=None, effect=None):
        values = self.cards()
        return tuple(card for card in values if self.card_filter.matches(
            card, source=source, player=player, session=session, effect=effect,
            **self.context))

    def validate(self, selected, **kwargs):
        allowed = {id(card): card for card in self.candidates(**kwargs)}
        return tuple(card for card in selected if id(card) in allowed)


def target_from_metadata(spec: dict, cards: Callable, *, context: dict | None = None):
    """Build a filter-backed target adapter from normalized target metadata."""
    if not isinstance(spec, dict):
        raise TypeError("target metadata must be a mapping")
    from .filters import filter_from_metadata
    filter_spec = spec.get("filter", spec.get("card_filter"))
    if filter_spec is None:
        raise ValueError("target metadata is missing filter")
    return FilteredTargetAdapter(filter_from_metadata(filter_spec), cards,
                                 context=context)
