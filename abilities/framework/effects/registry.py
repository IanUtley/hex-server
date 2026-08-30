"""Registry for metadata effect-type executors."""


_LEAFS = {}


def leaf_register(type_name):
    """Register a function for an ``ability_effects.effect_type``."""
    def decorator(fn):
        _LEAFS[type_name] = fn
        return fn
    return decorator

