from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

# Runtime skills are deliberately enumerated. Candidate and work artifacts are
# private workflow data and must never be discovered by a directory glob.
ACTIVE_SKILL_MANIFEST: dict[str, str] = {
    "buyer": "buyer.md",
    "seller": "seller.md",
    "portfolio": "portfolio.md",
    "general": "general.md",
    "unknown": "unknown.md",
    "skill_reviewer": "skill_reviewer.md",
}


class SkillHeader(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    release: str = Field(min_length=1, max_length=64)
    intent: str = Field(pattern=r"^(buyer|seller|portfolio|general|unknown|skill_reviewer)$")
    triggers: list[str] = Field(min_length=1)
    required_fields: list[str] = Field(default_factory=list)
    optional_fields: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class Skill:
    header: SkillHeader
    instructions: str


class SkillValidationError(ValueError):
    pass


def load_skill(path: Path) -> Skill:
    text = path.read_text(encoding="utf-8")
    return parse_skill_text(text, path.name)


def parse_skill_text(text: str, filename: str) -> Skill:
    match = re.match(r"\A---\s*\n(?P<header>.*?)\n---\s*\n(?P<body>.*)\Z", text, re.S)
    if match is None:
        raise SkillValidationError(f"{filename}: missing YAML front matter")
    # Keep the skill format intentionally small and dependency-free.
    values: dict[str, object] = {}
    for line in match.group("header").splitlines():
        if not line.strip():
            continue
        key, separator, value = line.partition(":")
        if not separator:
            raise SkillValidationError(f"{filename}: invalid header line")
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            values[key.strip()] = [
                part.strip().strip("\"'") for part in value[1:-1].split(",") if part.strip()
            ]
        else:
            values[key.strip()] = value.strip("\"'")
    try:
        header = SkillHeader.model_validate(values)
    except ValidationError as exc:
        raise SkillValidationError(f"{filename}: invalid skill metadata") from exc
    if header.key != Path(filename).stem:
        raise SkillValidationError(f"{filename}: key must match filename")
    body = match.group("body").strip()
    if not body:
        raise SkillValidationError(f"{filename}: empty instructions")
    return Skill(header=header, instructions=body)


def load_skills(directory: Path) -> dict[str, Skill]:
    directory = Path(directory)
    if any(part.casefold() in {"candidate", "candidates", "work"} for part in directory.parts):
        raise SkillValidationError("candidate and work directories are not runtime skill sources")
    if not directory.is_dir():
        raise SkillValidationError(f"active skill directory does not exist: {directory}")

    resolved_directory = directory.resolve()
    missing = [
        key
        for key, filename in ACTIVE_SKILL_MANIFEST.items()
        if not (directory / filename).is_file()
    ]
    if missing:
        raise SkillValidationError(f"missing required skills: {', '.join(sorted(missing))}")

    skills: dict[str, Skill] = {}
    for key, filename in ACTIVE_SKILL_MANIFEST.items():
        path = directory / filename
        if path.is_symlink() or path.resolve().parent != resolved_directory:
            raise SkillValidationError(
                f"{filename}: active skill must be a regular file in {directory}"
            )
        skill = load_skill(path)
        if skill.header.intent != key:
            raise SkillValidationError(f"{filename}: intent must match active skill key {key}")
        skills[key] = skill
    return skills


def select_skill(text: str, skills: dict[str, Skill]) -> Skill:
    normalized = text.casefold()
    matched = [
        skill
        for skill in skills.values()
        if skill.header.intent != "skill_reviewer"
        and any(trigger.casefold() in normalized for trigger in skill.header.triggers)
    ]
    if len(matched) > 1:
        return skills["unknown"]
    return matched[0] if matched else skills["unknown"]
