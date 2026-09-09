"""Registry for metadata effect-type executors."""

from functools import wraps

_LEAFS = {}


def _legacy_args(context):
    return (
        context.game, context.session, context.db, context.handler,
        context.player_uid, context.ai_uid, context.bstate,
        context.effect_guid, context.param,
    )


def _adapt(fn, *, context_style):
    """Give every registered leaf one context-shaped invocation boundary."""
    from ..context import EffectContext

    @wraps(fn)
    def adapter(*args, **kwargs):
        # New resolver code passes the context directly.  A compatibility
        # registration made with ``leaf_register`` still receives the old
        # positional ABI inside its adapter, so no caller needs to know which
        # implementation style is underneath.
        if (len(args) == 1 and isinstance(args[0], EffectContext)
                and not kwargs):
            return (fn(args[0]) if context_style
                    else fn(*_legacy_args(args[0])))
        if kwargs:
            return fn(*args, **kwargs)
        if len(args) != 9:
            raise TypeError(
                f"{fn.__name__} expects EffectContext or legacy leaf ABI")
        context = EffectContext.from_legacy(*args)
        return fn(context) if context_style else fn(*_legacy_args(context))

    return adapter


def leaf_register(type_name):
    """Register a legacy leaf behind the context-shaped registry boundary."""
    def decorator(fn):
        adapter = _adapt(fn, context_style=False)
        _LEAFS[type_name] = adapter
        return adapter
    return decorator


def effect(type_name):
    """Register a context-style effect while preserving direct old callers."""
    def decorator(fn):
        adapter = _adapt(fn, context_style=True)
        _LEAFS[type_name] = adapter
        return adapter
    return decorator
