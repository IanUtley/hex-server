"""Compatibility entry points for RulesPort resource ability handling."""

from typing import Any, Iterable


def printed_resource_choice_ability(
        ability_guids: Iterable[object] | None) -> str | None:
    from rules_port.resources import printed_resource_choice_ability as impl
    return impl(ability_guids)


def resolve_granted_resource_abilities(
        game: Any, session: Any, db: Any, handler: Any, pl_t: Any, ai_t: Any,
        bstate: Any, card_uid: Any, owner_id: Any, *,
        resolver: Any = None) -> Any:
    from rules_port.resources import resolve_granted_resource_abilities as impl
    return impl(game, session, db, handler, pl_t, ai_t, bstate, card_uid,
                owner_id, resolver=resolver)


def resolve_printed_resource_abilities(
        game: Any, session: Any, db: Any, handler: Any, pl_t: Any, ai_t: Any,
        bstate: Any, card_uid: Any, owner_id: Any,
        skip_guids: Iterable[object] = ()) -> Any:
    from rules_port.resources import resolve_printed_resource_abilities as impl
    return impl(game, session, db, handler, pl_t, ai_t, bstate, card_uid,
                owner_id, skip_guids=skip_guids)
