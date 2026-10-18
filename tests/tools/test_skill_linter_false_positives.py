"""Regression tests for the linter's false positives on shipped skills (issue #131754).

The bundled set reported 41 findings over 24 skills, and part of that count was the linter
misreading correct prose:

- ``## When To Use`` is the standard's own heading spelled in title case, but the match was
  case-sensitive;
- the sentences that tell an author to prefer the native tool — ``Prefer this over `cat` `` —
  and the table rows that enumerate a skill's shell dependencies were flagged as steering the
  model to the shell, which is the opposite of what they say;
- ``references/...`` inside a ``skill_view(file_path=...)`` example is a placeholder, not a path.

The ``missing-metadata`` warnings on the two skills that still declare top-level ``tags:`` are
deliberately NOT relaxed here: the fix in flight for those moves the tags under
``metadata.hermes`` (#127139), and the rule keeps its current strictness.

The corpus cases below run the linter over the real shipped files those false positives came
from, so the same reading cannot come back unnoticed. They deliberately assert one rule at a
time: these skills still carry other, genuine debt (oversized bodies, scaffolding READMEs,
missing triggers) that this change does not touch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.skill_linter import lint_content, lint_skill

CLEAN = """---
name: my-skill
description: Search arXiv papers by keyword, author, or ID.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [arxiv, research]
    related_skills: []
---

# My Skill

## Overview
Does a thing.

## When to Use
- When the user wants X.

## Procedure
1. Use `read_file` to load it.
"""

REPO = Path(__file__).resolve().parents[2]


def _rules(content: str) -> set[str]:
    return {f.rule for f in lint_content(content)}


def test_when_to_use_heading_is_case_insensitive():
    for heading in ("## When To Use", "## WHEN TO USE", "## When To Use This Skill"):
        content = CLEAN.replace("## When to Use", heading)
        assert "missing-section" not in _rules(content), heading


def test_legacy_top_level_tags_still_warns():
    # Not touched by this change: the rule keeps requiring metadata.hermes.tags.
    # The two shipped skills still on the legacy spelling are handled by the open
    # PR that moves them under metadata.hermes (#127139), not by loosening the rule.
    content = """---
name: my-skill
description: Search arXiv papers by keyword, author, or ID.
version: 1.0.0
author: Hermes Agent
license: MIT
tags: [arxiv, research]
---

# My Skill

## When to Use
- When the user wants X.
"""
    assert "missing-metadata" in _rules(content)


def test_shell_utility_named_beside_the_native_tool_is_not_flagged():
    # The standard asks for exactly this sentence ("`grep` -> `search_files`").
    content = CLEAN.replace(
        "1. Use `read_file` to load it.",
        "Do NOT name shell utilities the agent has wrapped (`grep` -> `search_files`).",
    )
    assert "shell-utility-reference" not in _rules(content)


def test_shell_utility_in_a_preference_sentence_is_not_flagged():
    content = CLEAN.replace(
        "1. Use `read_file` to load it.", "Prefer `read_file` over `cat` for line numbers."
    )
    assert "shell-utility-reference" not in _rules(content)


def test_shell_utility_in_a_table_row_is_not_flagged():
    content = CLEAN + "\n| bash pipelines, `grep`/`awk` chains | `[linux, macos]` |\n"
    assert "shell-utility-reference" not in _rules(content)


def test_bare_shell_utility_mention_still_flagged():
    content = CLEAN.replace("1. Use `read_file` to load it.", "Use `grep` to find it.")
    assert "shell-utility-reference" in _rules(content)


def test_placeholder_reference_is_not_dangling(tmp_path):
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    content = CLEAN + '\nLoad on demand: skill_view(name="my-skill", file_path="references/...").\n'
    rules = {f.rule for f in lint_content(content, skill_dir=skill_dir)}
    assert "dangling-reference" not in rules


@pytest.mark.parametrize(
    ("skill_dir", "rule"),
    [
        ("skills/creative/claude-design", "missing-section"),
        ("skills/note-taking/obsidian", "shell-utility-reference"),
        ("skills/research/arxiv", "shell-utility-reference"),
        ("skills/software-development/hermes-agent-skill-authoring", "shell-utility-reference"),
        ("skills/creative/p5js", "dangling-reference"),
    ],
)
def test_shipped_skill_is_not_false_positived(skill_dir, rule):
    rules = {f.rule for f in lint_skill(REPO / skill_dir / "SKILL.md")}
    assert rule not in rules, f"{skill_dir} should not report {rule}: {sorted(rules)}"
