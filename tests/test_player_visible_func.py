"""对齐 chaldea describeFunctions 玩家视角过滤的单元测试。

参考实现：
- chaldea/lib/app/descriptors/func/func.dart::describe (L53-152)
- chaldea/lib/models/gamedata/func.dart::isPlayerOnlyFunc / isEnemyOnlyFunc (L421-426)

默认场景：showPlayer=True, showEnemy=False（从者技能 / 宝具页）。
"""

from server.data_loader import is_player_visible_func


def _func(func_type: str = "addState", target_team: str = "playerAndEnemy", target_type: str = "self") -> dict:
    return {"funcType": func_type, "funcTargetTeam": target_team, "funcTargetType": target_type}


class TestFuncTypeNone:
    """funcType=none 必定丢弃。"""

    def test_none_dropped_even_with_team_all(self):
        assert is_player_visible_func(_func(func_type="none", target_team="playerAndEnemy")) is False

    def test_none_dropped_with_player_team(self):
        assert is_player_visible_func(_func(func_type="none", target_team="player", target_type="self")) is False


class TestPlayerAndEnemyTeam:
    """funcTargetTeam=playerAndEnemy（FuncApplyTarget.all）→ 一律保留。"""

    def test_keeps_self_target(self):
        assert is_player_visible_func(_func(target_team="playerAndEnemy", target_type="self")) is True

    def test_keeps_enemy_target(self):
        # 即便目标是敌方，只要 team=all 仍然保留（如清状态、降攻）
        assert is_player_visible_func(_func(target_team="playerAndEnemy", target_type="enemyAll")) is True


class TestPlayerOnlyFuncs:
    """isPlayerOnlyFunc：玩家持有时生效，showPlayer=True 时保留。"""

    def test_player_team_with_ally_target_kept(self):
        # team=player + targetType=self/pt* → 玩家视角保留
        assert is_player_visible_func(_func(target_team="player", target_type="self")) is True
        assert is_player_visible_func(_func(target_team="player", target_type="ptAll")) is True
        assert is_player_visible_func(_func(target_team="player", target_type="ptOne")) is True

    def test_enemy_team_with_enemy_target_kept(self):
        # team=enemy + targetType=enemy* → 玩家持有时打敌方，保留
        assert is_player_visible_func(_func(target_team="enemy", target_type="enemyAll")) is True
        assert is_player_visible_func(_func(target_team="enemy", target_type="enemy")) is True

    def test_player_only_dropped_when_show_player_false(self):
        assert is_player_visible_func(_func(target_team="player", target_type="self"), show_player=False) is False


class TestEnemyOnlyFuncs:
    """isEnemyOnlyFunc：敌方持有时生效，showEnemy=False 时丢弃。

    莎乐美三技能「七面纱舞」case：funcTargetTeam=enemy, funcTargetType=self
    → 敌方持有时给敌方加 buff，玩家视角应剔除。
    """

    def test_enemy_team_with_ally_target_dropped(self):
        # team=enemy + targetType=self/pt* → 敌方持有时给己方（敌方）加 buff
        assert is_player_visible_func(_func(target_team="enemy", target_type="self")) is False
        assert is_player_visible_func(_func(target_team="enemy", target_type="ptAll")) is False

    def test_player_team_with_enemy_target_dropped(self):
        # team=player + targetType=enemy* → 玩家持有时却作用敌方？（罕见但符合定义）
        assert is_player_visible_func(_func(target_team="player", target_type="enemyAll")) is False

    def test_enemy_only_kept_when_show_enemy_true(self):
        # 敌方页面 / 关卡分析时可启用
        assert (
            is_player_visible_func(_func(target_team="enemy", target_type="self"), show_enemy=True, show_player=False)
            is True
        )


class TestNeutralTargets:
    """field / dynamic / noTarget 等通用目标 → 保留。"""

    def test_field_other_kept(self):
        assert is_player_visible_func(_func(target_team="player", target_type="fieldOther")) is True

    def test_no_target_kept(self):
        assert is_player_visible_func(_func(target_team="player", target_type="noTarget")) is True

    def test_enemy_one_no_target_no_action_kept(self):
        # FuncTargetType.enemyOneNoTargetNoAction 被 chaldea 显式排除在 isEnemy 之外
        assert is_player_visible_func(_func(target_team="player", target_type="enemyOneNoTargetNoAction")) is True


class TestSalomeRegression:
    """莎乐美三技能「七面纱舞」回归用例：

    源数据有 4 条 functions：
      [0] regainHp, team=playerAndEnemy, target=self           → 保留
      [1] selfturnendFunction, team=playerAndEnemy, target=self → 保留
      [2] delayFunction (funcId=3871), team=player, target=self → 保留
      [3] delayFunction (funcId=3887), team=enemy, target=self → 丢弃 ✅
    """

    def test_salome_seven_veils_filtering(self):
        funcs = [
            {"funcType": "regainHp", "funcTargetTeam": "playerAndEnemy", "funcTargetType": "self"},
            {"funcType": "addState", "funcTargetTeam": "playerAndEnemy", "funcTargetType": "self"},
            {"funcType": "addState", "funcTargetTeam": "player", "funcTargetType": "self"},
            {"funcType": "addState", "funcTargetTeam": "enemy", "funcTargetType": "self"},
        ]
        kept = [f for f in funcs if is_player_visible_func(f)]
        assert len(kept) == 3
        assert kept[3 - 1]["funcTargetTeam"] == "player"  # 第 3 条保留
