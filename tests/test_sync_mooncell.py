"""
Mooncell 昵称同步脚本单元测试。

测试范围：
- wikitext 解析逻辑（昵称提取、序号提取）
- 昵称清洗（逗号分割、括号处理）
- collectionNo 映射逻辑
- 冲突检测
"""

from extractor.sync_mooncell_nicknames import (
    generate_nickname_mapping,
    parse_nicknames_from_wikitext,
)


class TestParseNicknames:
    """测试 wikitext 昵称解析。"""

    def test_basic_nicknames(self):
        """基本昵称提取。"""
        wikitext = "|序号=59\n|昵称=村姑,贞日天,尺子\n|立绘1=第1阶段"
        nicknames, collection_no = parse_nicknames_from_wikitext(wikitext)
        assert collection_no == 59
        assert nicknames == ["村姑", "贞日天", "尺子"]

    def test_single_nickname(self):
        """单个昵称。"""
        wikitext = "|序号=100\n|昵称=杀狐\n"
        nicknames, collection_no = parse_nicknames_from_wikitext(wikitext)
        assert collection_no == 100
        assert nicknames == ["杀狐"]

    def test_empty_nickname_field(self):
        """昵称字段为空。"""
        wikitext = "|序号=42\n|昵称=\n|立绘1=第1阶段"
        nicknames, collection_no = parse_nicknames_from_wikitext(wikitext)
        assert collection_no == 42
        assert nicknames == []

    def test_no_nickname_field(self):
        """无昵称字段。"""
        wikitext = "|序号=10\n|中文名=赤兔马\n"
        nicknames, collection_no = parse_nicknames_from_wikitext(wikitext)
        assert collection_no == 10
        assert nicknames == []

    def test_no_collection_no(self):
        """无序号字段。"""
        wikitext = "|昵称=测试昵称\n"
        nicknames, collection_no = parse_nicknames_from_wikitext(wikitext)
        assert collection_no is None
        assert nicknames == ["测试昵称"]

    def test_chinese_comma_separator(self):
        """中文逗号分割。"""
        wikitext = "|序号=1\n|昵称=盾娘，学妹，茄子\n"
        nicknames, collection_no = parse_nicknames_from_wikitext(wikitext)
        assert "盾娘" in nicknames
        assert "学妹" in nicknames
        assert "茄子" in nicknames

    def test_parenthetical_expansion(self):
        """括号补充说明会额外提取短形式。"""
        wikitext = "|序号=118\n|昵称=拉二（拉美西斯二世）,王中王\n"
        nicknames, collection_no = parse_nicknames_from_wikitext(wikitext)
        assert "拉二（拉美西斯二世）" in nicknames
        assert "拉二" in nicknames
        assert "王中王" in nicknames

    def test_parenthetical_no_duplicate(self):
        """括号短形式与原始相同时不重复。"""
        wikitext = "|序号=50\n|昵称=测试\n"
        nicknames, _ = parse_nicknames_from_wikitext(wikitext)
        assert nicknames.count("测试") == 1

    def test_whitespace_trimming(self):
        """昵称两端空白应被去除。"""
        wikitext = "|序号=5\n|昵称= 红A , 红弓 \n"
        nicknames, _ = parse_nicknames_from_wikitext(wikitext)
        assert "红A" in nicknames
        assert "红弓" in nicknames


class TestGenerateMapping:
    """测试昵称映射生成。"""

    def _make_lookup(self, entries: list[tuple[int, str, str]]) -> dict[int, dict]:
        """构造 servant_lookup: [(collectionNo, name, className), ...]"""
        return {no: {"name": name, "className": cls} for no, name, cls in entries}

    def test_basic_mapping(self):
        """基本映射生成。"""
        page_contents = {
            "贞德": "|序号=59\n|昵称=村姑,尺子\n",
        }
        lookup = self._make_lookup([(59, "贞德", "ruler")])

        mapping, stats = generate_nickname_mapping(page_contents, lookup)

        assert "村姑" in mapping
        assert mapping["村姑"]["name"] == "贞德"
        assert mapping["村姑"]["className"] == "ruler"
        assert stats["total_nickname_entries"] == 2

    def test_conflict_detection(self):
        """冲突检测：同一昵称指向不同从者。"""
        page_contents = {
            "牛若丸(Rider)": "|序号=100\n|昵称=牛若\n",
            "牛若丸(Assassin)": "|序号=200\n|昵称=牛若\n",
        }
        lookup = self._make_lookup(
            [
                (100, "牛若丸", "rider"),
                (200, "牛若丸", "assassin"),
            ]
        )

        mapping, stats = generate_nickname_mapping(page_contents, lookup)

        assert "牛若" in mapping
        assert isinstance(mapping["牛若"], list)
        assert len(mapping["牛若"]) == 2
        assert len(stats["conflicts"]) == 1

    def test_same_servant_no_conflict(self):
        """同一从者多个页面出现相同昵称不算冲突。"""
        page_contents = {
            "页面A": "|序号=59\n|昵称=村姑\n",
            "页面B": "|序号=59\n|昵称=村姑\n",
        }
        lookup = self._make_lookup([(59, "贞德", "ruler")])

        mapping, stats = generate_nickname_mapping(page_contents, lookup)

        assert "村姑" in mapping
        assert isinstance(mapping["村姑"], dict)  # 不是 list
        assert len(stats["conflicts"]) == 0

    def test_skip_no_collection_no(self):
        """无序号的页面应被跳过。"""
        page_contents = {
            "某页面": "|昵称=测试昵称\n",
        }
        lookup = self._make_lookup([(1, "某从者", "saber")])

        mapping, stats = generate_nickname_mapping(page_contents, lookup)

        assert "测试昵称" not in mapping
        assert stats["pages_no_collection_no"] == 1

    def test_skip_unmapped_collection_no(self):
        """序号在 servants_db 中不存在时跳过。"""
        page_contents = {
            "未知从者": "|序号=9999\n|昵称=测试\n",
        }
        lookup = self._make_lookup([(1, "某从者", "saber")])

        mapping, stats = generate_nickname_mapping(page_contents, lookup)

        assert "测试" not in mapping
        assert stats["pages_unmapped"] == 1

    def test_pages_without_nicknames(self):
        """无昵称的页面统计。"""
        page_contents = {
            "赤兔马": "|序号=10\n|中文名=赤兔马\n",
        }
        lookup = self._make_lookup([(10, "赤兔马", "rider")])

        mapping, stats = generate_nickname_mapping(page_contents, lookup)

        assert len(mapping) == 0
        assert stats["pages_without_nicknames"] == 1
