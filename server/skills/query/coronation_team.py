"""
Laplace — Coronation Team Skill

查询戴冠战配队推荐（按职阶/流派/角色分类）。
数据源: server/config/coronation/team/{className}.json
自动附带 boss 机制摘要作为上下文。
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from server.skills.base import QuerySkill, register_skill

CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config" / "coronation"

# 七骑标准职阶集合
_STANDARD_CLASSES = frozenset({"saber", "archer", "lancer", "rider", "caster", "assassin", "berserker"})

# 中文职阶名 → 英文文件名映射
CLASS_NAME_MAP = {
    "剑": "saber",
    "剑阶": "saber",
    "弓": "archer",
    "弓阶": "archer",
    "枪": "lancer",
    "枪阶": "lancer",
    "骑": "rider",
    "骑阶": "rider",
    "术": "caster",
    "术阶": "caster",
    "杀": "assassin",
    "杀阶": "assassin",
    "狂": "berserker",
    "狂阶": "berserker",
}


class CoronationTeamParams(BaseModel):
    """戴冠战配队推荐参数。"""

    className: str
    playstyle: str | None = None
    role: str | None = None


@register_skill
class CoronationTeamSkill(QuerySkill):
    """戴冠战配队推荐检索。"""

    name = "coronation_team"
    description = "查询戴冠战配队推荐(按职阶/流派/角色分类)"
    domain = "coronation"

    @property
    def params_schema(self) -> type[BaseModel] | None:
        return CoronationTeamParams

    def execute(self, db: list[dict], params: dict) -> list[dict]:
        """执行配队推荐检索。返回结构化推荐数据。"""
        class_name = params.get("className", "")
        playstyle = params.get("playstyle")
        role = params.get("role")

        # 解析职阶名
        resolved = CLASS_NAME_MAP.get(class_name, class_name).lower()
        team_file = CONFIG_DIR / "team" / f"{resolved}.json"

        if not team_file.exists():
            supported = self._get_supported_classes()
            return [
                {
                    "type": "not_found",
                    "message": f"{class_name}阶戴冠战配队数据暂未收录。目前已收录: {supported}",
                }
            ]

        with open(team_file, encoding="utf-8") as f:
            data = json.load(f)

        result: dict = {
            "type": "team",
            "className": data.get("className"),
            "title": data.get("title"),
            "difficulty": data.get("difficulty"),
        }

        # 过滤流派
        playstyles = data.get("playstyles", [])
        if playstyle:
            playstyles = [p for p in playstyles if playstyle in p.get("name", "")]
        result["playstyles"] = playstyles

        # 过滤角色分类
        role_categories = data.get("roleCategories", [])
        if role:
            role_categories = [rc for rc in role_categories if role in rc.get("role", "")]

        # 运行时职阶校验：剔除 collectionNo 在 db 中不匹配本职阶的从者
        role_categories = self._filter_by_class(role_categories, resolved, db)
        result["roleCategories"] = role_categories

        # 精简 playstyles：移除 requirements/tips 减少 token
        for ps in result.get("playstyles", []):
            ps.pop("requirements", None)
            ps.pop("tips", None)

        # 附带 Boss 机制摘要
        boss_summary = self._load_boss_summary(resolved)
        if boss_summary:
            result["bossSummary"] = boss_summary

        return [result]

    def _filter_by_class(self, role_categories: list[dict], target_class: str, db: list[dict]) -> list[dict]:
        """运行时职阶校验：剔除 collectionNo 在 db 中不匹配本职阶的从者。

        规则：
        - 七骑职阶(saber/archer/...)：从者 className 必须完全匹配
        - EX 职阶：从者 className 不在七骑中即可（允许所有非七骑 EX 职阶）
        - collectionNo 为 None 的从者（泛称条目如"冠位从者们"）跳过校验
        """
        is_ex = target_class not in _STANDARD_CLASSES

        # db 为空时跳过校验（测试场景或数据未加载时）
        if not db:
            return role_categories

        # 构建 collectionNo → className 快速查找表
        cno_to_class: dict[int, str] = {}
        for s in db:
            cno = s.get("collectionNo")
            if cno is not None:
                cno_to_class[cno] = s.get("className", "").lower()

        filtered_categories: list[dict] = []
        for rc in role_categories:
            valid_servants = []
            for servant in rc.get("servants", []):
                cno = servant.get("collectionNo")
                if cno is None:
                    # 泛称条目（如"冠位从者们(剑阶全员)"），跳过校验
                    valid_servants.append(servant)
                    continue
                db_class = cno_to_class.get(cno)
                if db_class is None:
                    # db 中找不到该 collectionNo，静默跳过
                    continue
                if is_ex:
                    # EX 戴冠：允许所有非七骑职阶
                    if db_class not in _STANDARD_CLASSES:
                        valid_servants.append(servant)
                else:
                    # 标准七骑：必须完全匹配
                    if db_class == target_class:
                        valid_servants.append(servant)

            if valid_servants:
                filtered_rc = {**rc, "servants": valid_servants}
                filtered_categories.append(filtered_rc)

        return filtered_categories

    def _load_boss_summary(self, class_name: str) -> dict | None:
        """加载 Boss 机制摘要（精简版）。"""
        boss_file = CONFIG_DIR / "boss" / f"{class_name}.json"
        if not boss_file.exists():
            return None

        with open(boss_file, encoding="utf-8") as f:
            boss = json.load(f)

        return {
            "bossName": boss.get("bossName"),
            "traits": boss.get("traits"),
            "attribute": boss.get("attribute"),
        }

    def _get_supported_classes(self) -> str:
        """获取当前已收录的职阶列表。"""
        team_dir = CONFIG_DIR / "team"
        if not team_dir.exists():
            return "无"
        files = list(team_dir.glob("*.json"))
        names = [f.stem for f in files]
        # 英文 → 中文映射
        en_to_cn = {
            "saber": "剑",
            "archer": "弓",
            "lancer": "枪",
            "rider": "骑",
            "caster": "术",
            "assassin": "杀",
            "berserker": "狂",
        }
        return "、".join(en_to_cn.get(n, n) for n in sorted(names))
