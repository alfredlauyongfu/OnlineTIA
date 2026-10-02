"""The extraction prompt's contract for actionable guidance.

Sheet help-text carries the steps a customer must take to reach the good rating.
The compaction rules exist to stop large sheets overflowing the response limit,
but they must not eat those steps — losing one turns a report's remediation into
a partial fix.
"""

from __future__ import annotations

from reference_sheet_extractor import ReferenceSheetExtractor


def _prompt() -> str:
    """The system prompt the extractor sends, however it is exposed."""
    for attr in ("SYSTEM_PROMPT", "EXTRACTION_SYSTEM_PROMPT", "_SYSTEM_PROMPT"):
        value = getattr(ReferenceSheetExtractor, attr, None)
        if isinstance(value, str):
            return value
    import reference_sheet_extractor as mod
    for name in dir(mod):
        value = getattr(mod, name)
        if isinstance(value, str) and "snake_case" in value and "COMPACT" in value:
            return value
    raise AssertionError("extraction system prompt not found")


def test_actionable_guidance_has_canonical_field_names() -> None:
    """One agreed key each, so downstream prompts can reference them. The sheets
    previously scattered this across note/notes/aim/context/reference."""
    p = _prompt()
    assert '"guidance"' in p
    assert '"remediation_steps"' in p
    assert "never synonyms" in p


def test_multi_action_guidance_must_keep_every_action() -> None:
    p = _prompt()
    assert "MORE THAN ONE required action" in p
    assert "preserving EVERY action" in p


def test_compaction_rule_excludes_actionable_content() -> None:
    """The size rule must cut explanation, not the actions themselves."""
    p = _prompt()
    assert "is NEVER dropped" in p
    assert "What gets cut is" in p


def test_full_question_wording_is_still_required() -> None:
    """The pre-existing contract the report's headings depend on."""
    p = _prompt()
    assert "VERBATIM" in p
    assert '"question"' in p
