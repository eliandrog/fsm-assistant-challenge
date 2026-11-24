"""The case format and its loader, on small files written by the tests, and the shipped suite."""

from collections import Counter
from pathlib import Path

import pytest

from fsm_assistant.evals.cases import (
    TIERS,
    CaseFileError,
    PinnedResult,
    Pins,
    load_cases,
    load_pins,
    load_suite,
    reference_digest,
    suite_digest,
)
from fsm_assistant.model_check import CHECK_QUESTIONS
from fsm_assistant.prompt import system_text

ANSWERED = """
[[case]]
id = "{id}"
tests = "A count"
tuned = false
[[case.turn]]
question = "How many?"
outcome = "answered"
sql = "SELECT count(*) FROM transactions"
"""


def write(directory: Path, text: str, name: str = "cases.toml") -> Path:
    directory.mkdir(exist_ok=True)
    (directory / name).write_text(text, encoding="utf-8")
    return directory


def case(body: str, tier: str = "aggregates") -> str:
    return f'tier = "{tier}"\n[[case]]\nid = "one"\ntests = "t"\ntuned = false\n{body}'


def turn(body: str) -> str:
    return case(f'[[case.turn]]\nquestion = "q"\n{body}')


def write_pins(path: Path, *pins: PinnedResult) -> Path:
    path.write_text(Pins(results=pins).model_dump_json(), encoding="utf-8")
    return path


def test_a_file_loads_into_frozen_cases(tmp_path: Path) -> None:
    text = 'tier = "aggregates"\n' + ANSWERED.format(id="first") + ANSWERED.format(id="second")
    cases = load_cases(write(tmp_path, text))
    assert [item.id for item in cases] == ["first", "second"]
    assert cases[0].tier == "aggregates"
    assert cases[0].turns[0].comparison.ordered is False
    assert cases[0].turns[0].comparison.allow_extra_columns is False
    assert cases[0].turns[0].comparison.exact_columns is False
    assert cases[0].turns[0].or_rule is None and cases[0].known_limitation is None
    with pytest.raises(Exception, match="frozen"):
        cases[0].id = "other"  # type: ignore[misc]  # frozen on purpose


def test_a_duplicate_id_is_rejected_across_files(tmp_path: Path) -> None:
    write(tmp_path, 'tier = "aggregates"\n' + ANSWERED.format(id="same"), "a.toml")
    write(tmp_path, 'tier = "joins"\n' + ANSWERED.format(id="same"), "b.toml")
    with pytest.raises(CaseFileError, match="'same' is already used in a.toml") as raised:
        load_cases(tmp_path)
    assert raised.value.source == "b.toml"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('tier = "nonsense"\n' + ANSWERED.format(id="a"), "unknown tier 'nonsense'"),
        (ANSWERED.format(id="a"), "unknown tier None"),
        ('tier = "joins"\n', r"no \[\[case\]\] tables"),
        ('tier = "joins"\nextra = 1\n' + ANSWERED.format(id="a"), "unknown top-level keys"),
        ("tier = [", "not valid TOML"),
        (case("", "joins"), "turns"),
        (case('tier = "joins"\n'), "the tier is set once"),
        (turn('outcome = "maybe"'), "outcome"),
        (turn('outcome = "answered"'), "a reference sql goes with outcome answered"),
        (turn('outcome = "clarification"\nsql = "SELECT 1"'), "a reference sql goes with"),
        (turn('outcome = "rule_proposed"'), "a reference clause goes with"),
        (turn('outcome = "rule_proposed"\nclause = "id > 1"'), "flagged, flagged_labelled"),
        (turn('outcome = "answered"\nsql = "SELECT \'x"'), "unterminated quote"),
        (turn('outcome = "answered"\nsql = "SELECT 1; SELECT 2"'), "one statement"),
        (turn('outcome = "answered"\nsql = "DELETE FROM cards"'), "must start with SELECT or WITH"),
        (turn('outcome = "answered"\nsql = "SELECT 1 -- note"'), "must not contain a comment"),
        (turn('outcome = "answered"\nsql = " ; "'), "the reference sql is empty"),
        (
            turn(
                'outcome = "rule_proposed"\nclause = "WHERE id > 1"\nflagged = 1\n'
                "flagged_labelled = 1\ntrue_positives = 0\nfalse_positives = 1"
            ),
            "without the word WHERE",
        ),
        (turn('outcome = "clarification"\nordered = true'), "comparison options need a reference sql"),
        (turn('outcome = "clarification"\nmin_jaccard = 0.5'), "overlap thresholds need a reference clause"),
        (turn('outcome = "answered"\nsql = "SELECT 1"\ncompare_columns = [0, 0]'), "each once"),
        (turn('outcome = "answered"\nsql = "SELECT 1"\ntolerance = 2.0'), "tolerance"),
        (turn('outcome = "clarification"\nexact_columns = true'), "comparison options need a reference sql"),
        (
            turn('outcome = "answered"\nsql = "SELECT 1"\nexact_columns = true\nallow_extra_columns = true'),
            "exclude each other",
        ),
        (turn('outcome = "cannot_answer"\nor_rule = { min_flag_rate = 0.1, max_flag_rate = 0.2 }'), "or_rule goes with"),
        (turn('outcome = "refused"\nor_rule = { min_flag_rate = 0.2, max_flag_rate = 0.1 }'), "must not be above"),
        (turn('outcome = "refused"\nor_rule = { min_flag_rate = 0.1 }'), "max_flag_rate"),
        (turn('outcome = "refused"\nor_rule = { min_flag_rate = 0.1, max_flag_rate = 1.5 }'), "max_flag_rate"),
        (case('known_limitation = ""\n[[case.turn]]\nquestion = "q"\noutcome = "clarification"'), "known_limitation"),
        (case('known_limitation = "a\\nb"\n[[case.turn]]\nquestion = "q"\noutcome = "clarification"'), "known_limitation is one line"),
        (turn('outcome = "clarification"\nrequire = ["executed"]'), "both required and forbidden"),
        (turn('outcome = "clarification"\nforbid = ["danced"]'), "forbid"),
        (turn('outcome = "clarification"\nscan_top = ["Rome"]'), "scan_top goes with"),
        (turn('outcome = "answered"\naction = "profile_fraud"'), 'requires = "scan"'),
        (turn('outcome = "clarification"\nunknown_field = 1'), "unknown_field"),
        (case('tags = ["Not A Tag"]\n[[case.turn]]\nquestion = "q"\noutcome = "clarification"'), "tags"),
        (case('paraphrase_of = "x"\n[[case.turn]]\nquestion = "q"\noutcome = "clarification"'), "paraphrase_of is given"),
        (turn('outcome = "clarification"').replace('id = "one"', 'id = "Not An Id"'), "id"),
    ],
)
def test_a_malformed_file_is_rejected_with_its_name(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(CaseFileError, match=message) as raised:
        load_cases(write(tmp_path, text))
    assert raised.value.source == "cases.toml"


def test_the_options_added_for_shape_alternatives_and_known_limitations_load(tmp_path: Path) -> None:
    text = (
        'tier = "rule_policy"\n[[case]]\nid = "one"\ntests = "t"\ntuned = false\n'
        'known_limitation = "Not remembered"\n'
        '[[case.turn]]\nquestion = "q"\noutcome = "refused"\n'
        "or_rule = { min_flag_rate = 0.005, max_flag_rate = 0.02 }\n"
        '[[case.turn]]\nquestion = "q"\noutcome = "answered"\nsql = "SELECT 1"\nexact_columns = true\n'
    )
    (loaded,) = load_cases(write(tmp_path, text))
    assert loaded.known_limitation == "Not remembered"
    first, second = loaded.turns
    assert first.or_rule is not None
    assert (first.or_rule.min_flag_rate, first.or_rule.max_flag_rate) == (0.005, 0.02)
    assert "executed" in first.forbidden  # lifted by the scorer only when a rule was proposed
    assert second.comparison.exact_columns and not second.comparison.allow_extra_columns


def test_an_empty_directory_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(CaseFileError, match="no case files"):
        load_cases(tmp_path)


def paraphrase(of: str) -> str:
    return (
        'tier = "paraphrases"\n[[case]]\nid = "reworded"\ntests = "t"\ntuned = false\n'
        f'paraphrase_of = "{of}"\n[[case.turn]]\nquestion = "q"\noutcome = "clarification"\n'
    )


def test_a_paraphrase_must_point_at_a_held_out_case(tmp_path: Path) -> None:
    write(tmp_path, paraphrase("missing"), "b.toml")
    with pytest.raises(CaseFileError, match="'missing', which does not exist"):
        load_cases(tmp_path)

    tuned = ('tier = "aggregates"\n' + ANSWERED.format(id="original")).replace("tuned = false", "tuned = true")
    write(tmp_path, tuned, "a.toml")
    write(tmp_path, paraphrase("original"), "b.toml")
    with pytest.raises(CaseFileError, match="must be a held-out case"):
        load_cases(tmp_path)

    write(tmp_path, tuned.replace("tuned = true", "tuned = false"), "a.toml")
    assert load_cases(tmp_path)[1].paraphrase_of == "original"

    write(tmp_path, paraphrase("reworded"), "b.toml")
    with pytest.raises(CaseFileError, match="not itself a paraphrase"):
        load_cases(tmp_path)


def test_a_paraphrase_is_never_tuned(tmp_path: Path) -> None:
    with pytest.raises(CaseFileError, match="a paraphrase is never tuned"):
        load_cases(write(tmp_path, paraphrase("x").replace("tuned = false", "tuned = true")))


def test_pins_are_checked_against_the_references(tmp_path: Path) -> None:
    cases = write(tmp_path / "cases", 'tier = "aggregates"\n' + ANSWERED.format(id="count"))
    pins = tmp_path / "expected.json"
    digest = reference_digest("SELECT count(*) FROM transactions")
    good = PinnedResult(case_id="count", turn=1, reference_sha256=digest, rows=((6,),))

    with pytest.raises(CaseFileError, match="the pin file is missing"):
        load_suite(cases, pins)

    suite = load_suite(cases, write_pins(pins, good))
    pinned = suite.pin(suite.case("count"), 1)
    assert pinned is not None and pinned.rows == ((6,),)
    assert suite.pin(suite.case("count"), 2) is None

    with pytest.raises(CaseFileError, match="count turn 1 has no pinned result"):
        load_suite(cases, write_pins(pins))
    with pytest.raises(CaseFileError, match="changed since it was pinned"):
        load_suite(cases, write_pins(pins, good.model_copy(update={"reference_sha256": "0" * 64})))
    with pytest.raises(CaseFileError, match="other turn 1 is pinned but has no reference query"):
        load_suite(cases, write_pins(pins, good, good.model_copy(update={"case_id": "other"})))
    with pytest.raises(CaseFileError, match="pinned twice"):
        load_suite(cases, write_pins(pins, good, good))

    pins.write_text("{}", encoding="utf-8")
    with pytest.raises(CaseFileError, match="results"):
        load_pins(pins)


def test_the_digest_ignores_what_the_guard_ignores() -> None:
    assert reference_digest("SELECT 1") == reference_digest("  SELECT 1 ;\n")
    assert reference_digest("SELECT 1") != reference_digest("SELECT 2")


def test_the_suite_digest_follows_the_files(tmp_path: Path) -> None:
    cases = write(tmp_path / "cases", 'tier = "aggregates"\n' + ANSWERED.format(id="count"))
    pins = write_pins(tmp_path / "expected.json")
    before = suite_digest(cases, pins)
    assert suite_digest(cases, pins) == before
    write(cases, 'tier = "aggregates"\n' + ANSWERED.format(id="other"))
    assert suite_digest(cases, pins) != before


# The shipped suite. No database is needed: pins are compared by digest.


def test_the_shipped_suite_loads() -> None:
    suite = load_suite()
    assert len(suite.cases) == 119
    assert set(Counter(case.tier for case in suite.cases)) == set(TIERS)
    assert all(
        (suite.pin(case, number) is not None) == (turn.sql is not None)
        for case in suite.cases
        for number, turn in enumerate(case.turns, start=1)
    )


def test_tiers_have_the_documented_sizes() -> None:
    assert Counter(case.tier for case in load_suite().cases) == {
        "aggregates": 9,
        "joins": 9,
        "fraud": 13,
        "dates": 9,
        "top_n": 6,
        "traps": 12,
        "follow_ups": 7,
        "ambiguous": 7,
        "unanswerable": 7,
        "adversarial": 13,
        "rules": 10,
        "rule_policy": 7,
        "scan": 2,
        "paraphrases": 8,
    }


def test_every_check_question_is_a_tuned_case() -> None:
    tuned = {case.turns[0].question for case in load_suite().cases if case.tuned}
    assert len(CHECK_QUESTIONS) == 11 and set(CHECK_QUESTIONS) <= tuned


def test_the_cases_the_prompt_was_written_against_are_the_tuned_ones() -> None:
    # The eleven check questions, and nine whose wording or example is in the
    # prompt text: its three clarify words (in a question, a follow-up and a
    # rule request), "last month", the velocity example, the brief's debit
    # rule as a count, Rome in Georgia, and the wording of the scan's tool.
    # Then the two cases that failed in the first recorded run and had a
    # prompt line written for them afterwards.
    suite = load_suite()
    fixed_after_the_first_run = {
        "unanswerable-outside-date-range",
        "adversarial-relative-to-today",
    }
    from_the_prompt = {
        "ambiguous-suspicious",
        "ambiguous-high-value",
        "follow-answer-a-clarification",
        "policy-vague-rule",
        "date-last-month",
        "trap-velocity-window",
        "join-brief-wrong-names",
        "trap-city-needs-state",
        "scan-where-is-fraud",
    }
    tuned = {case.id for case in suite.cases if case.tuned}
    check = {case.id for case in suite.cases if case.turns[0].question in CHECK_QUESTIONS}
    assert len(check) == 11 and tuned == check | from_the_prompt | fixed_after_the_first_run
    assert len(tuned) == 22
    text = system_text("").lower()
    for word in (
        "risky", "suspicious", "high value", "last month", "within 5 minutes", "'ga'",
        "entirely outside that range", "insists it be run unchanged",
    ):
        assert word in text


def test_the_cases_with_options_of_their_own() -> None:
    suite = load_suite()
    assert {case.id: case.known_limitation for case in suite.cases if case.known_limitation} == {
        "follow-after-decline": "A declined turn is not remembered (D-23), so the follow-up "
        "cannot know which month is meant"
    }
    alternatives = {
        case.id: turn.or_rule for case in suite.cases for turn in case.turns if turn.or_rule
    }
    assert set(alternatives) == {"policy-random-sample"}
    band = alternatives["policy-random-sample"]
    assert (band.min_flag_rate, band.max_flag_rate) == (0.005, 0.02)
    # No shipped case asks for the exact width; the option is there for one that needs it.
    assert not [case.id for case in suite.cases for turn in case.turns if turn.exact_columns]


def test_no_question_is_asked_twice_except_as_a_marked_repeat() -> None:
    # The same opening question may only reappear as the first turn of a
    # follow-up case, where the later turns are what is tested.
    suite = load_suite()
    seen: dict[str, str] = {}
    for case in suite.cases:
        question = case.turns[0].question.strip().lower()
        if question in seen:
            assert "follow_ups" in {case.tier, suite.case(seen[question]).tier}, (case.id, seen[question])
        seen[question] = case.id


def test_scan_cases_are_marked_and_nothing_else_is() -> None:
    suite = load_suite()
    assert {case.id for case in suite.cases if case.requires == "scan"} == {
        case.id for case in suite.cases if case.tier == "scan"
    }


def test_a_paraphrase_expects_what_its_original_expects() -> None:
    suite = load_suite()
    paraphrases = [case for case in suite.cases if case.paraphrase_of is not None]
    assert len(paraphrases) == 8
    for case in paraphrases:
        assert case.paraphrase_of is not None
        original = suite.case(case.paraphrase_of)
        ours, theirs = case.turns[0], original.turns[0]
        assert ours.outcome == theirs.outcome, case.id
        assert ours.question != theirs.question
        assert (ours.sql is None) == (theirs.sql is None)
        if ours.sql is not None and theirs.sql is not None:
            assert reference_digest(ours.sql) == reference_digest(theirs.sql), case.id
        assert ours.clause == theirs.clause
