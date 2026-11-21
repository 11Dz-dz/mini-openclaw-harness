import pytest

from miniclaw.skills.matcher import SkillMatcher
from miniclaw.skills.types import Skill


def skills():
    return (Skill('release', '撰写发布说明和变更记录', ('发布说明', 'release'),
                  'project', 'release instructions', 'project:release'),
            Skill('review', '检查项目代码质量', ('代码审查',),
                  'project', 'review instructions', 'project:review'))


@pytest.mark.parametrize(('prompt', 'expected'), [
    ('请帮我撰写发布说明', 'release'),
    ('请对这个项目做代码审查', 'review'),
    ('帮我整理变更记录', 'release'),
    ('write release notes', 'release'),
    ('解释releaseable这个词', None),
    ('今天北京天气怎么样', None),
])
def test_small_labelled_matching_regression_set(prompt, expected):
    matches = SkillMatcher().match(prompt).select(skills())
    assert (matches[0].skill.name if matches else None) == expected
