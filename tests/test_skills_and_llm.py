from pathlib import Path
from shutil import copyfile

import pytest

from app.integrations.bedrock.provider import FakeLlm
from app.skills.loader import SkillValidationError, load_skill, load_skills, select_skill

SKILLS = Path(__file__).parents[1] / "skills"


def test_all_versioned_skills_load_and_buyer_routes() -> None:
    skills = load_skills(SKILLS)
    selected = select_skill("I am looking to buy an apartment", skills)
    assert selected.header.key == "buyer"
    assert selected.header.release == "4"


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


def test_candidate_and_work_markdown_are_excluded_from_runtime_loading(tmp_path: Path) -> None:
    active_directory = tmp_path / "active-skill-runtime"
    active_directory.mkdir()
    for skill_file in SKILLS.glob("*.md"):
        copyfile(skill_file, active_directory / skill_file.name)
    (active_directory / "buyer_conversation_skill_99.md").write_text(
        "candidate content", encoding="utf-8"
    )
    for directory in ("candidates/buyer", "work"):
        artifact_dir = active_directory / directory
        artifact_dir.mkdir(parents=True)
        (artifact_dir / "untrusted.md").write_text("untrusted content", encoding="utf-8")

    loaded = load_skills(active_directory)
    assert set(loaded) == {
        "buyer",
        "seller",
        "portfolio",
        "general",
        "unknown",
        "skill_reviewer",
    }


def test_runtime_loader_fails_with_clear_missing_skill_error(tmp_path: Path) -> None:
    active_directory = tmp_path / "skills"
    active_directory.mkdir()
    for skill_file in SKILLS.glob("*.md"):
        if skill_file.name != "unknown.md":
            copyfile(skill_file, active_directory / skill_file.name)

    with pytest.raises(SkillValidationError, match="missing required skills: unknown"):
        load_skills(active_directory)
