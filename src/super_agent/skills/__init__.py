"""Skills: reusable, user-selectable playbooks the Worker can load on demand.

A *tool* is one callable action. A *skill* is a folder of instructions plus
optional bundled resources and helper scripts that teaches the model *how* to
do a whole class of job (write a release note, audit a repository, build a
landing page). Tools are what the agent can do; skills are what it knows.

The distinction matters for context budget: a skill's body is only injected
when it is selected, so a library of thirty skills costs nothing until one is
chosen.
"""

from super_agent.skills.loader import (
    Skill,
    SkillLoadReport,
    SkillRegistry,
    build_skill_registry,
    discover_skill_dirs,
    parse_skill_file,
    select_skills,
)
from super_agent.skills.tools import SkillListTool, SkillReadTool

__all__ = [
    "Skill",
    "SkillListTool",
    "SkillLoadReport",
    "SkillReadTool",
    "SkillRegistry",
    "build_skill_registry",
    "discover_skill_dirs",
    "parse_skill_file",
    "select_skills",
]
