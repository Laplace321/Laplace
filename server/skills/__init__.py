"""
Laplace — Skills Package

导入所有 Skill 模块以触发 @register_skill 装饰器注册。
"""

import importlib as _importlib

_SKILL_MODULES = [
    # Query Skills
    "server.skills.query.compare_servants",
    "server.skills.query.lookup_servant",
    "server.skills.query.search_by_attribute",
    "server.skills.query.search_by_cards",
    "server.skills.query.search_by_class",
    "server.skills.query.search_by_np_charge",
    "server.skills.query.search_by_np_effect",
    "server.skills.query.search_by_rarity",
    "server.skills.query.search_by_skill_effect",
    "server.skills.query.search_by_traits",
    # Response Skills
    "server.skills.response.respond_servant_compare",
    "server.skills.response.respond_servant_detail",
    "server.skills.response.respond_servant_list",
    "server.skills.response.respond_support_analysis",
]

for _mod in _SKILL_MODULES:
    _importlib.import_module(_mod)
