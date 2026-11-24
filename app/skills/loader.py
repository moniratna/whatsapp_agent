from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError


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
    match = re.match(r"\A---\s*\n(?P<header>.*?)\n---\s*\n(?P<body>.*)\Z", text, re.S)
    if match is None:
        raise SkillValidationError(f"{path.name}: missing YAML front matter")
    # Keep the skill format intentionally small and dependency-free.
    values: dict[str, object] = {}
    for line in match.group("header").splitlines():
        if not line.strip():
            continue
        key, separator, value = line.partition(":")
        if not separator:
            raise SkillValidationError(f"{path.name}: invalid header line")
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
        raise SkillValidationError(f"{path.name}: invalid skill metadata") from exc
    if header.key != path.stem:
        raise SkillValidationError(f"{path.name}: key must match filename")
    body = match.group("body").strip()
    if not body:
        raise SkillValidationError(f"{path.name}: empty instructions")
    return Skill(header=header, instructions=body)


def load_skills(directory: Path) -> dict[str, Skill]:
    skills = {path.stem: load_skill(path) for path in sorted(directory.glob("*.md"))}
    required = {"buyer", "seller", "portfolio", "general", "unknown", "skill_reviewer"}
    missing = required - skills.keys()
    if missing:
        raise SkillValidationError(f"missing required skills: {', '.join(sorted(missing))}")
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
