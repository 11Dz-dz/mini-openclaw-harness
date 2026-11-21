from pathlib import Path

from miniclaw.skills.parser import SkillParseError, SkillParser
from miniclaw.skills.types import Skill


def _discover(root: Path, scope: str, diagnostics: list) -> tuple[Skill, ...]:
    if not root.exists():
        return ()
    resolved_root = root.resolve()
    skills = []
    for path in sorted(root.glob("*/SKILL.md")):
        resolved = path.resolve()
        if not resolved.is_relative_to(resolved_root):
            continue
        try:
            with path.open('rb') as file:
                text = file.read(65537).decode('utf-8')
            skills.append(SkillParser().parse(text, source_id=f"{scope}:{path.parent.name}"))
        except (SkillParseError, OSError, UnicodeError) as error:
            diagnostics.append({'source': str(path), 'error': type(error).__name__,
                                'detail': str(error) if isinstance(error, SkillParseError)
                                else 'skill file could not be read'})
    return tuple(skills)


def discover_skills(
    project_root: Path,
    *,
    user_root: Path | None = None,
    diagnostics: list | None = None,
) -> tuple[Skill, ...]:
    diagnostics = diagnostics if diagnostics is not None else []
    project_skills = _discover(
        project_root / ".miniclaw" / "skills",
        "project",
        diagnostics,
    )
    user_skills = _discover(
        user_root or Path.home() / ".miniclaw" / "skills",
        "user",
        diagnostics,
    )
    by_name = {skill.name: skill for skill in user_skills}
    by_name.update({skill.name: skill for skill in project_skills})
    return tuple(by_name[name] for name in sorted(by_name))
