"""RulesPort-owned pending-chain state primitives.

The persisted ``stack`` list is intentionally kept wire/database compatible.
Only the phase/turn host may project it; chain mutation itself belongs here so
RulesPort and its compatibility callers cannot grow separate implementations.
"""

from __future__ import annotations


PLAYER = "player"
AI = "ai"


def push(state, item):
    state.setdefault("stack", []).append(item)


def pop(state):
    stack = state.get("stack") or []
    return stack.pop() if stack else None


def top(state):
    stack = state.get("stack") or []
    return stack[-1] if stack else None


def empty(state):
    return not (state.get("stack") or [])


def clear(state):
    state["stack"] = []


def set_pass(state, player, passed):
    key = "stack_player_passed" if str(player).lower() == PLAYER \
        else "stack_ai_passed"
    state[key] = bool(passed)


def both_passed(state):
    return bool(state.get("stack_player_passed")) and bool(
        state.get("stack_ai_passed"))


def reset_passes(state):
    state["stack_player_passed"] = False
    state["stack_ai_passed"] = False
