"""Skill: 按职阶筛选从者。"""

import re

from pydantic import BaseModel, ConfigDict, Field

from server.skills.base import QuerySkill, register_skill

# 职阶别名表：覆盖玩家圈常见叫法，避免 translations.json 映射括号注释导致反查失败。
# value 必须与 servants_db.json 中 servant.className 一致（camelCase）。
CLASS_ALIASES: dict[str, str] = {
    # 三骑
    "saber": "saber",
    "剑阶": "saber",
    "剑兵": "saber",
    "archer": "archer",
    "弓阶": "archer",
    "弓兵": "archer",
    "lancer": "lancer",
    "枪阶": "lancer",
    "枪兵": "lancer",
    # 四骑
    "rider": "rider",
    "骑阶": "rider",
    "骑兵": "rider",
    "caster": "caster",
    "魔术师": "caster",
    "术阶": "caster",
    "术士": "caster",
    "assassin": "assassin",
    "杀阶": "assassin",
    "暗杀者": "assassin",
    "刺客": "assassin",
    "berserker": "berserker",
    "狂阶": "berserker",
    "狂战士": "berserker",
    # 特阶
    "ruler": "ruler",
    "裁定者": "ruler",
    "裁定": "ruler",
    "督阶": "ruler",
    "avenger": "avenger",
    "复仇者": "avenger",
    "复阶": "avenger",
    "仇阶": "avenger",
    "mooncancer": "moonCancer",
    "月之癌": "moonCancer",
    "月癌": "moonCancer",
    "月阶": "moonCancer",
    "alterego": "alterEgo",
    "他人格": "alterEgo",
    "人魔": "alterEgo",
    "他我": "alterEgo",
    "foreigner": "foreigner",
    "降临者": "foreigner",
    "外来者": "foreigner",
    "辱阶": "foreigner",
    "pretender": "pretender",
    "伪装者": "pretender",
    "伪阶": "pretender",
    "偊阶": "pretender",
    "shielder": "shielder",
    "盾阶": "shielder",
    "盾兵": "shielder",
    "beast": "beast",
    "兽阶": "beast",
    "兽": "beast",
}


def _normalize(text: str) -> str:
    """去括号注释（如 「月癌(MoonCancer)」 → 「月癌」）与多余空白，统一小写。"""
    if not text:
        return ""
    # 去除中英文括号及其内容
    text = re.sub(r"[\(（][^\)）]*[\)）]", "", text)
    return text.strip().lower()


class Params(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    class_name: str = Field(alias="className")


@register_skill
class SearchByClass(QuerySkill):
    name = "search_by_class"
    description = "按职阶筛选从者，支持中文（如「狂阶」「术阶」「月之癌」）或英文（如 Saber、Caster）"
    domain = "servant"

    @property
    def params_schema(self) -> type[BaseModel]:
        return Params

    def _resolve(self, raw: str) -> str:
        """将任意形式的职阶输入归一化为 servant.className 的标准表示。"""
        if not raw:
            return raw
        norm = _normalize(raw)
        # 1) 别名表直接命中（中文 / 全英文小写）
        if norm in CLASS_ALIASES:
            return CLASS_ALIASES[norm]
        # 2) 反查 translations.json 取原始中文（能处理“狂阶”这类未括号词条）
        from server.translation import get_class_map

        cn_to_en = {_normalize(cn): en for en, cn in get_class_map().items()}
        if norm in cn_to_en:
            # 返回 translations 原始 key（如 mooncancer），后续还需映射为 camelCase
            base = cn_to_en[norm]
            return CLASS_ALIASES.get(base, base)
        # 3) 原样返回，交由下游 lower() 匹配
        return raw

    def filter(self, servant: dict, params: dict) -> bool:
        class_name = params.get("class_name")
        if class_name is None:
            return True
        target = self._resolve(class_name.strip()).lower()
        return servant.get("className", "").lower() == target
