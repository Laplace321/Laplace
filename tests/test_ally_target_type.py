"""Tests for the 'ally' query-side targetType value.

ally = party + ptOne + partyOther (all ways to benefit teammates, excluding pure self).
"""

from server.query_executor import _match_effect, _match_np_effect, _match_target_type
from server.translation import effect_qualifier

# ---------------------------------------------------------------------------
# _match_target_type
# ---------------------------------------------------------------------------


class TestMatchTargetTypeAlly:
    """ally should match party, ptOne, partyOther but NOT self."""

    def test_ally_matches_party(self):
        assert _match_target_type("ally", "party") is True

    def test_ally_matches_pt_one(self):
        assert _match_target_type("ally", "ptOne") is True

    def test_ally_matches_party_other(self):
        assert _match_target_type("ally", "partyOther") is True

    def test_ally_does_not_match_self(self):
        assert _match_target_type("ally", "self") is False

    def test_ally_does_not_match_enemy(self):
        assert _match_target_type("ally", "enemy") is False


class TestMatchTargetTypePartyUnchanged:
    """Verify party's original semantics are preserved (no ptOne matching)."""

    def test_party_matches_party(self):
        assert _match_target_type("party", "party") is True

    def test_party_matches_party_other(self):
        assert _match_target_type("party", "partyOther") is True

    def test_party_does_not_match_pt_one(self):
        assert _match_target_type("party", "ptOne") is False

    def test_party_does_not_match_self(self):
        assert _match_target_type("party", "self") is False


# ---------------------------------------------------------------------------
# Fixture: servant with mixed targetType effects
# ---------------------------------------------------------------------------

_SERVANT_WITH_MIXED_GAIN_NP = {
    "name": "TestServant",
    "skillEffects": ["gainNp"],
    "npEffects": ["gainNp"],
    "skillDetails": [
        {
            "skillName": "Skill1",
            "effects": [
                {"type": "gainNp", "targetType": "party", "valueMax": 100},
            ],
        },
        {
            "skillName": "Skill2",
            "effects": [
                {"type": "gainNp", "targetType": "ptOne", "valueMax": 300},
            ],
        },
    ],
    "npDetails": [
        {
            "npName": "NP1",
            "effects": [
                {"type": "gainNp", "targetType": "partyOther", "valueLv1": 200},
            ],
        },
    ],
}

_SERVANT_SELF_ONLY = {
    "name": "SelfCharger",
    "skillEffects": ["gainNp"],
    "npEffects": [],
    "skillDetails": [
        {
            "skillName": "Skill1",
            "effects": [
                {"type": "gainNp", "targetType": "self", "valueMax": 500},
            ],
        },
    ],
    "npDetails": [],
}


# ---------------------------------------------------------------------------
# _match_effect with ally
# ---------------------------------------------------------------------------


class TestMatchEffectAlly:
    """ally should accumulate party + ptOne + partyOther values."""

    def test_ally_matches_servant_with_party_and_pt_one(self):
        result = _match_effect(_SERVANT_WITH_MIXED_GAIN_NP, "gainNp", target_type="ally")
        assert result is True

    def test_ally_does_not_match_self_only_servant(self):
        result = _match_effect(_SERVANT_SELF_ONLY, "gainNp", target_type="ally")
        assert result is False

    def test_ally_with_min_value_pass(self):
        # party(100) + ptOne(300) = 400, min_value=300 (converted to 3000‰ internally? no — values already in ‰)
        # valueMax is already in ‰: party=100, ptOne=300, total=400
        result = _match_effect(_SERVANT_WITH_MIXED_GAIN_NP, "gainNp", target_type="ally", min_value=400)
        assert result is True

    def test_ally_with_min_value_fail(self):
        # party(100) + ptOne(300) = 400, min_value=500 → fail
        result = _match_effect(_SERVANT_WITH_MIXED_GAIN_NP, "gainNp", target_type="ally", min_value=500)
        assert result is False


class TestMatchEffectPartyUnchanged:
    """Verify party query does NOT include ptOne (original behavior preserved)."""

    def test_party_excludes_pt_one(self):
        # party(100) only, ptOne(300) excluded → total=100
        result = _match_effect(_SERVANT_WITH_MIXED_GAIN_NP, "gainNp", target_type="party", min_value=200)
        assert result is False

    def test_party_matches_party_value(self):
        result = _match_effect(_SERVANT_WITH_MIXED_GAIN_NP, "gainNp", target_type="party", min_value=100)
        assert result is True


# ---------------------------------------------------------------------------
# _match_np_effect with ally
# ---------------------------------------------------------------------------


class TestMatchNpEffectAlly:
    """ally should work symmetrically for NP effects."""

    def test_ally_matches_np_party_other(self):
        result = _match_np_effect(_SERVANT_WITH_MIXED_GAIN_NP, "gainNp", target_type="ally")
        assert result is True

    def test_ally_does_not_match_self_only_np(self):
        result = _match_np_effect(_SERVANT_SELF_ONLY, "gainNp", target_type="ally")
        assert result is False


# ---------------------------------------------------------------------------
# effect_qualifier translation
# ---------------------------------------------------------------------------


class TestEffectQualifierAlly:
    def test_ally_qualifier(self):
        result = effect_qualifier({"targetType": "ally"})
        assert result == "队友的"

    def test_party_qualifier_unchanged(self):
        result = effect_qualifier({"targetType": "party"})
        assert result == "全队的"

    def test_self_qualifier_unchanged(self):
        result = effect_qualifier({"targetType": "self"})
        assert result == "自身的"
