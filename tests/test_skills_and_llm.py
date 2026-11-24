from pathlib import Path

import pytest

from app.integrations.bedrock.provider import FakeLlm
from app.skills.loader import SkillValidationError, load_skill, load_skills, select_skill

SKILLS = Path(__file__).parents[1] / "skills"


def test_all_versioned_skills_load_and_buyer_routes() -> None:
    skills = load_skills(SKILLS)
    selected = select_skill("I am looking to buy an apartment", skills)
    assert selected.header.key == "buyer"
    assert selected.header.release == "1"


def test_sensitive_or_human_request_routes_to_handover() -> None:
    skills = load_skills(SKILLS)
    selected = select_skill("I want to speak to a lawyer", skills)
    result = FakeLlm().decide(skill=selected, inbound="I want to speak to a lawyer", transcript=[])
    assert result.decision == "handover"


def test_unmatched_question_routes_to_unknown_skill() -> None:
    skills = load_skills(SKILLS)
    assert select_skill("Can you provide a legal valuation?", skills).header.key == "unknown"


def test_invalid_skill_metadata_is_rejected(tmp_path: Path) -> None:
    skill_file = tmp_path / "buyer.md"
    skill_file.write_text("---\nkey: wrong\n---\nBody", encoding="utf-8")
    with pytest.raises(SkillValidationError):
        load_skill(skill_file)
