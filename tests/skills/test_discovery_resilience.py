import json

from miniclaw.app import create_app
from miniclaw.cli.repl import AppRepl
from miniclaw.config import MiniClawConfig
from miniclaw.core.messages import ResponseCompleted, TextDelta
from miniclaw.model.fake import FakeModel


async def test_bad_skill_is_reported_while_valid_skill_still_reaches_model(tmp_path):
    project = tmp_path / 'project'
    root = project / '.miniclaw' / 'skills'
    (root / 'bad').mkdir(parents=True)
    (root / 'bad' / 'SKILL.md').write_text('broken frontmatter')
    (root / 'good').mkdir()
    (root / 'good' / 'SKILL.md').write_text(
        '---\nname: good\ndescription: specific workflow\n---\nUse this exact procedure.')
    model = FakeModel((TextDelta('ok'), ResponseCompleted('stop')))
    app = await create_app(MiniClawConfig(tmp_path / 'data', project), provider=model)
    try:
        errors = json.loads((await app.handle_command('/skills errors')).output)
        assert len(errors) == 1
        assert errors[0]['error'] == 'SkillParseError'
        assert 'skill not found' in (await app.handle_command('/skill missing')).output
        await AppRepl(app, write=lambda text: None).process('/skill good perform the task')
        assert any('Use this exact procedure' in repr(m) for m in model.requests[-1].messages)
    finally:
        await app.aclose()


async def test_double_slash_sends_literal_command_to_model(tmp_path):
    model = FakeModel((ResponseCompleted('stop'),))
    app = await create_app(MiniClawConfig(tmp_path / 'data', tmp_path / 'project'), provider=model)
    try:
        await AppRepl(app, write=lambda text: None).process('//help explain this command')
        assert model.requests[-1].messages[-1].content[0].text == '/help explain this command'
    finally:
        await app.aclose()
