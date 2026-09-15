"""Tests for the persisted intake artefacts in `intake/` —
`online_tia_form.json` (the Microsoft Form) and `power_automate_flow.json` (the
flow that turns a submission into the pipeline's input JSON).

Both live outside the repo in SaaS tools; these files are the repo's record of
them, so the intake chain can be recreated elsewhere. The tests guard that
record: they fail if either artefact is malformed, if questions the pipeline
depends on disappear, if Microsoft-internal fields leak back in, or if the flow
and the form drift apart.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest


INTAKE_DIR = Path(__file__).resolve().parent.parent / "intake"
FORM_PATH = INTAKE_DIR / "online_tia_form.json"
FLOW_PATH = INTAKE_DIR / "power_automate_flow.json"

# Counts as extracted from the live form. Update deliberately — a change here
# should accompany a real questionnaire change.
EXPECTED_QUESTIONS = 35
EXPECTED_SECTIONS = 4

# Fields the pipeline reads out of a submitted response. "Booking ID" names the
# report (`TIA_<Booking ID>_<ts>`) and "Organisation" fills the .docx cover —
# see tia_generator._extract_cover_meta. Matched loosely on question title
# because the Power Automate flow, not the form, defines the export key names.
# ("Submission time" is Forms-generated metadata, not a question, so it is not
# checked here.)
REQUIRED_QUESTION_KEYWORDS = ("booking id", "organisation")

# Stripped during extraction; their reappearance means raw Microsoft output
# was committed instead of the cleaned form.
FORBIDDEN_KEYS = (
    "id", "ownerId", "ownerTenantId", "trackingId", "tableId", "collectionId",
    "modifiedDate", "status", "insightsInfo", "questionInfo",
    "deserializedQuestionInfo", "titleHasPhishingKeywords",
)


@pytest.fixture(scope="module")
def form() -> dict:
    return json.loads(FORM_PATH.read_text(encoding="utf-8"))


def _questions(form: dict) -> list[dict]:
    return [i for i in form["items"] if i["kind"] == "question"]


def test_form_definition_parses_with_expected_top_level_keys(form: dict) -> None:
    assert set(form) >= {"source", "title", "description", "settings", "items"}
    assert form["source"]["url"].startswith("https://forms.office.com/")
    assert form["title"].strip()


def test_description_carries_the_enterprise_only_scope(form: dict) -> None:
    """The header must keep stating that the assessment excludes Blue Prism
    Cloud, and must keep the privacy-policy link.

    The author's text contains non-breaking spaces (U+00A0), which the artefact
    preserves verbatim; they are normalised here so the check is about wording,
    not invisible whitespace.
    """
    description = form["description"].replace("\xa0", " ")
    assert "Blue Prism Enterprise" in description
    assert "does NOT apply to Blue Prism Cloud" in description
    assert "ssctech.com/about/privacy" in description


def test_question_and_section_counts(form: dict) -> None:
    sections = [i for i in form["items"] if i["kind"] == "section"]
    assert len(_questions(form)) == EXPECTED_QUESTIONS
    assert len(sections) == EXPECTED_SECTIONS


def test_every_question_is_well_formed(form: dict) -> None:
    for q in _questions(form):
        assert q["title"].strip(), f"untitled question: {q}"
        assert q["type"] in ("text", "choice"), f"unknown type in {q['title']!r}"
        assert isinstance(q["required"], bool)
        if q["type"] == "choice":
            assert q["choices"], f"choice question with no options: {q['title']!r}"
            assert all(c.strip() for c in q["choices"]), f"blank option in {q['title']!r}"
            assert isinstance(q["allowMultipleSelection"], bool)
            assert isinstance(q["allowOtherAnswer"], bool)
        else:
            assert isinstance(q["multiline"], bool)
            assert isinstance(q["isNumber"], bool)


@pytest.mark.parametrize("keyword", REQUIRED_QUESTION_KEYWORDS)
def test_pipeline_dependency_questions_present(form: dict, keyword: str) -> None:
    """Removing these from the form would break report naming / the .docx cover."""
    titles = [q["title"].lower() for q in _questions(form)]
    assert any(keyword in t for t in titles), f"no question mentions {keyword!r}"


def test_no_microsoft_internal_fields_leaked(form: dict) -> None:
    for item in form["items"]:
        leaked = sorted(set(item) & set(FORBIDDEN_KEYS))
        assert not leaked, f"internal field(s) {leaked} in {item.get('title')!r}"


# ---------- the Power Automate flow, and its agreement with the form ----------

@pytest.fixture(scope="module")
def flow_fields() -> dict:
    """The `Response_Data` object the flow writes as the pipeline's input JSON.

    The exported flow is deliberately NOT committed — it carries tenant and
    subscription GUIDs, the internal SharePoint path and staff email addresses,
    and this repository is public. Export it from Power Automate to
    `intake/power_automate_flow.json` to run these drift checks locally.
    """
    if not FLOW_PATH.is_file():
        pytest.skip(f"{FLOW_PATH.name} is kept out of this public repo; "
                    "export the flow locally to run the flow/form drift tests")
    flow = json.loads(FLOW_PATH.read_text(encoding="utf-8"))
    actions = flow["properties"]["definition"]["actions"]
    return actions["Initialize_variable"]["inputs"]["variables"][0]["value"][0]


def test_flow_emits_the_nested_question_answer_shape(flow_fields: dict) -> None:
    """Every answered field carries its question; only submission metadata is
    flat. This is the contract tia_generator._extract_fields relies on."""
    nested = {k: v for k, v in flow_fields.items() if isinstance(v, dict)}
    flat = {k: v for k, v in flow_fields.items() if not isinstance(v, dict)}
    assert len(nested) == EXPECTED_QUESTIONS
    assert set(flat) == {"Submission time"}
    for key, entry in nested.items():
        assert set(entry) == {"question", "answer"}, f"bad field shape: {key}"
        # The answer must still be bound to a Forms question, not hard-coded.
        assert re.search(r"body/r[0-9a-f]{32}", entry["answer"]), key


def test_flow_questions_match_the_form_verbatim(form: dict, flow_fields: dict) -> None:
    """Drift guard: editing a question in Forms without updating the flow (or
    vice versa) fails here rather than surfacing as odd report headings."""
    form_questions = [q["title"] for q in _questions(form)]
    emitted = [v["question"] for v in flow_fields.values() if isinstance(v, dict)]
    assert sorted(emitted) == sorted(form_questions)


def test_flow_reads_both_network_latency_questions(flow_fields: dict) -> None:
    """The form asks latency twice (SQL Server and Interactive Client). The flow
    once collapsed both onto a single key, silently dropping one answer."""
    latency = {k: v for k, v in flow_fields.items() if "latency" in k.lower()}
    assert len(latency) == 2, f"expected two latency fields, got {list(latency)}"
    guids = {re.search(r"body/(r[0-9a-f]{32})", v["answer"]).group(1)
             for v in latency.values()}
    assert len(guids) == 2, "both latency fields read the same form question"
    subjects = " ".join(latency).lower()
    assert "sql server" in subjects and "interactive client" in subjects


def test_flow_preserves_multi_select_joins(flow_fields: dict) -> None:
    """Multi-select answers arrive as a JSON array and must stay join()-ed into
    a single string, or the pipeline would receive raw array text."""
    joined = [k for k, v in flow_fields.items()
              if isinstance(v, dict) and "join(json(" in v["answer"]]
    multi = [q["title"] for q in _questions(json.loads(
        FORM_PATH.read_text(encoding="utf-8"))) if q.get("allowMultipleSelection")]
    assert len(joined) == len(multi), f"{len(joined)} join()s for {len(multi)} multi-selects"
