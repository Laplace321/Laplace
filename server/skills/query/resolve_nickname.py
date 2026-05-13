"""Skill: LLM 智能昵称识别。

当 lookup_servant 通过本地昵称表无法匹配时，调用 LLM 识别昵称/外号对应的正式从者名称。
结合内存 LRU 缓存和 DB 存在性校验，确保准确率和性能。
"""

from __future__ import annotations

from collections import OrderedDict

from pydantic import BaseModel, Field

from server.query_executor import _normalize_text, load_database
from server.skills.base import QuerySkill, register_skill

# ── LRU 缓存 ──

_CACHE_MAX_SIZE = 256
_nickname_cache: OrderedDict[str, str | None] = OrderedDict()


def _cache_get(key: str) -> tuple[bool, str | None]:
    """LRU 缓存读取。返回 (是否命中, 缓存值)。"""
    if key in _nickname_cache:
        _nickname_cache.move_to_end(key)
        return True, _nickname_cache[key]
    return False, None


def _cache_set(key: str, value: str | None) -> None:
    """LRU 缓存写入。仅缓存成功结果（value 非 None）。"""
    if value is None:
        return
    _nickname_cache[key] = value
    _nickname_cache.move_to_end(key)
    while len(_nickname_cache) > _CACHE_MAX_SIZE:
        _nickname_cache.popitem(last=False)


# ── Prompt ──

_SYSTEM_PROMPT = (
    "你是 FGO（Fate/Grand Order）从者数据库助手。"
    "用户会给你一个从者的昵称或外号，你需要返回该从者在数据库中的**简短中文名称**。\n"
    "规则：\n"
    "1. 只返回从者的简短中文名称，不要有任何其他文字、解释或标点。\n"
    "2. 如果你不确定这个昵称对应哪个从者，返回「unknown」。\n"
    "3. 优先使用简短常用名，**不要**使用全名。\n"
    "4. 正确示例：梅林、卫宫、吉尔伽美什、诸葛孔明、斯卡蒂、伊什塔尔、贞德。\n"
    "5. 错误示例（太长）：阿尔托莉雅·潘德拉贡〔弓〕、亚历山大大帝。\n"
    "6. 关键映射参考：红A/红弓=卫宫、花之魔术师=梅林、闪闪/金闪闪=吉尔伽美什、"
    "大帝=伊斯坎达尔、孔明=诸葛孔明、杰克=开膛手杰克、弓凛=伊什塔尔。"
)


def _build_user_message(nickname: str) -> str:
    """构建用户消息。"""
    return f"FGO 中「{nickname}」是哪个从者？"


# ── DB 校验 ──


def _validate_in_db(resolved_name: str) -> list[dict]:
    """在 servants_db 中校验 LLM 返回的名称是否存在。

    使用与 lookup_servant 相同的模糊匹配逻辑。
    返回匹配到的从者列表。
    """
    db = load_database()
    normalized_resolved = _normalize_text(resolved_name)

    if not normalized_resolved or normalized_resolved == "unknown":
        return []

    matches = []
    for servant in db:
        en_name = servant.get("name", "")
        cn_name = servant.get("aliasCN", "")
        jp_name = servant.get("originalName", "")
        norm_en = _normalize_text(en_name)
        norm_cn = _normalize_text(cn_name)
        norm_jp = _normalize_text(jp_name)

        # 精确匹配
        if normalized_resolved in (norm_en, norm_cn, norm_jp):
            matches.append(servant)
            continue

        # 子串匹配（LLM 返回的名称可能是部分名）
        if len(normalized_resolved) >= 2:
            if normalized_resolved in norm_cn or normalized_resolved in norm_en or normalized_resolved in norm_jp:
                matches.append(servant)

    return matches


# ── Skill 定义 ──


class Params(BaseModel):
    """resolve_nickname 参数。"""

    name: str = Field(description="从者昵称/外号")


@register_skill
class ResolveNickname(QuerySkill):
    """通过 LLM 识别从者昵称/外号对应的正式名称。"""

    name = "resolve_nickname"
    description = "通过 LLM 识别从者昵称/外号对应的正式名称（lookup_servant 未命中时自动触发）"
    domain = "servant"

    @property
    def params_schema(self) -> type[BaseModel]:
        return Params

    def execute(self, db: list[dict], params: dict) -> list[dict]:
        """同步执行（仅缓存命中时有效，LLM 调用走 execute_async）。"""
        nickname = params.get("name", "").strip()
        if not nickname:
            return []

        # 仅检查 LRU 缓存（同步路径）
        cache_key = _normalize_text(nickname)
        hit, cached_name = _cache_get(cache_key)
        if hit and cached_name:
            return _validate_in_db(cached_name)

        # 未命中缓存时，同步路径无法调用 LLM，返回空
        # 生产环境通过 execute_async() 调用
        return []

    async def execute_async(self, db: list[dict], params: dict) -> list[dict]:
        """异步执行（支持 LLM 调用，用于 FastAPI 环境）。"""
        nickname = params.get("name", "").strip()
        if not nickname:
            return []

        # 1. 检查 LRU 缓存
        cache_key = _normalize_text(nickname)
        hit, cached_name = _cache_get(cache_key)
        if hit and cached_name:
            return _validate_in_db(cached_name)

        # 2. 调用 LLM 识别
        resolved_name = await self._call_llm_async(nickname)
        if not resolved_name or resolved_name.strip().lower() == "unknown":
            return []

        resolved_name = resolved_name.strip()

        # 3. DB 存在性校验
        matches = _validate_in_db(resolved_name)
        if matches:
            # 校验通过，写入缓存
            _cache_set(cache_key, resolved_name)

        return matches

    async def _call_llm_async(self, nickname: str) -> str | None:
        """异步调用 LLM。"""
        from server.llm import chat_completion

        try:
            result = await chat_completion(
                system_prompt=_SYSTEM_PROMPT,
                user_message=_build_user_message(nickname),
                max_tokens=64,
                temperature=0.0,
                json_mode=False,
            )
            # json_mode=False 时返回 {"text": "..."}
            text = result.get("text", "").strip()
            # 清洗：移除可能的引号和多余标点
            text = text.strip("\"'「」『』")
            return text if text else None
        except Exception as e:
            print(f"⚠️  [resolve_nickname] LLM 调用异常: {e}")
            return None
