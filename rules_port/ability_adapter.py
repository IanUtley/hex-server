"""Compatibility adapter for the pre-RulesPort metadata resolver API."""

from .resolution import PortAbilityResolver
from .actions import AbilityResolutionState


class MetadataResolutionAdapter(PortAbilityResolver):
    """Deprecated test/rollback adapter; live RulesPort is native-only."""

    def __init__(self, handler, game, game_session, db, player_uid, ai_uid,
                 battle_state, *, resolver=None, native_effect=None,
                 effect_groups=None):
        if resolver is None:
            super().__init__(handler, game, game_session, db, player_uid, ai_uid,
                             battle_state, native_effect=native_effect,
                             effect_groups=effect_groups)
            self._compat_resolver = None
        else:
            self.handler, self.game = handler, game
            self.game_session, self.db = game_session, db
            self.player_uid, self.ai_uid = player_uid, ai_uid
            self.battle_state = battle_state
            self.native_effect = native_effect
            self.effect_groups = effect_groups
            self._compat_resolver = resolver

    def __call__(self, ability):
        if self._compat_resolver is None:
            return super().__call__(ability)
        self._compat_resolver(
            self.handler, self.game, self.game_session, self.db,
            self.player_uid, self.ai_uid, self.battle_state,
            ability.ability_template_id, ability.source_uid,
            ability.responsible_player_id,
            target_map=ability.activation.target_map,
            variables=ability.activation.variables,
            activation_data=ability.activation.as_dict(),
            effect_groups=self.effect_groups,
            native_effect=self.native_effect,
        )
        return AbilityResolutionState.COMPLETED
