"""
Laplace — Coronation Knowledge Skill

查询戴冠战通用知识（机制/星图/礼装/刷取策略）或 Boss 机制。
数据源: server/config/coronation/guide.json + boss/{className}.json
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from server.skills.base import QuerySkill, register_skill

CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config" / "coronation"


class CoronationKnowledgeParams(BaseModel):
    """戴冠战知识查询参数。"""

    topic: str | None = None
    className: str | None = None


@register_skill
class CoronationKnowledgeSkill(QuerySkill):
    """戴冠战通用知识 + Boss 机制检索。"""

    name = "coronation_knowledge"
    description = "查询戴冠战通用知识(机制/星图/礼装/刷取)或Boss机制"
    domain = "coronation"

    @property
    def params_schema(self) -> type[BaseModel] | None:
        return CoronationKnowledgeParams

    def execute(self, db: list[dict], params: dict) -> list[dict]:
        """执行知识检索。返回知识条目列表（非从者列表）。"""
        topic = params.get("topic")
        class_name = params.get("className")

        # Boss 机制查询
        if topic == "boss" and class_name:
            return self._load_boss(class_name)

        # 通用知识查询
        return self._load_guide(topic)

    def _load_boss(self, class_name: str) -> list[dict]:
        """加载指定职阶的 Boss 机制数据。"""
        # 支持中文职阶名映射
        class_map = {
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
        resolved = class_map.get(class_name, class_name).lower()
        boss_file = CONFIG_DIR / "boss" / f"{resolved}.json"

        if not boss_file.exists():
            return [
                {
                    "type": "not_found",
                    "message": f"{class_name}阶戴冠战 Boss 数据暂未收录，目前仅支持: 剑阶",
                }
            ]

        with open(boss_file, encoding="utf-8") as f:
            data = json.load(f)

        return [{"type": "boss", "data": data}]

    def _load_guide(self, topic: str | None) -> list[dict]:
        """加载通用知识条目。"""
        guide_file = CONFIG_DIR / "guide.json"

        if not guide_file.exists():
            return [{"type": "error", "message": "戴冠战知识库文件缺失"}]

        with open(guide_file, encoding="utf-8") as f:
            guide = json.load(f)

        entries = guide.get("entries", [])

        if topic:
            # 精确匹配 topic 字段
            matched = [e for e in entries if e.get("topic") == topic]
            if matched:
                return [{"type": "guide", "entries": matched}]
            # 降级: keywords 子串匹配
            matched = [e for e in entries if any(topic in kw for kw in e.get("keywords", []))]
            if matched:
                return [{"type": "guide", "entries": matched}]
            # 无匹配
            return [
                {
                    "type": "guide",
                    "entries": entries,
                    "note": f"未找到与'{topic}'精确匹配的条目，返回全部知识供参考",
                }
            ]

        # 无 topic 时返回全部
        return [{"type": "guide", "entries": entries}]
