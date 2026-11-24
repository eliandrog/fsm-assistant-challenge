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
from fsm_assistant.prompt import SCAN_RESULT_UNSEEN, system_text

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


RULE = (
    'outcome = "rule_proposed"\nclause = "id > 1"\nflagged = 5\nflagged_labelled = 4\n'
    "true_positives = 1\nfalse_positives = 3\n"
)


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
        (turn('outcome = "clarification"\nweakest_month = "2019-03"'), "need a reference clause"),
        (turn('outcome = "clarification"\nmonths = [["2019-03", 1, 1, 1, 0]]'), "need a reference clause"),
        (turn(RULE + 'weakest_month = "March"'), "weakest_month"),
        (turn(RULE + "months = []"), "months"),
        (turn(RULE + 'months = [["2019-3", 5, 4, 1, 3]]'), "months"),
        (turn(RULE + 'months = [["2019-03", 5, 4, 1]]'), "months"),
        (turn(RULE + 'months = [["2019-03", 5, 4, 1, 3], ["2019-03", 0, 0, 0, 0]]'), "once each, in order"),
        (turn(RULE + 'months = [["2019-04", 3, 2, 1, 1], ["2019-03", 2, 2, 0, 2]]'), "once each, in order"),
        (turn(RULE + 'months = [["2019-03", 3, 2, 1, 1], ["2019-04", 2, 2, 0, 1]]'), "do not add up"),
        (turn('outcome = "clarification"\nrequire = ["executed"]'), "both required and forbidden"),
        (turn('outcome = "clarification"\nforbid = ["danced"]'), "forbid"),
        (turn('outcome = "clarification"\nscan_top = ["Rome"]'), "scan_top goes with"),
        (turn('outcome = "answered"\naction = "profile_fraud"'), 'requires = "scan"'),
        (turn('outcome = "clarification"\nunknown_field = 1'), "unknown_field"),
        (case('tags = ["Not A Tag"]\n[[case.turn]]\nquestion = "q"\noutcome = "clarification"'), "tags"),
        (case('paraphrase_of = "x"\n[[case.turn]]\nquestion = "q"\noutcome = "clarification"'), "paraphrase_of is given"),
        (turn('outcome = "clarification"').replace('id = "one"', 'id = "Not An Id"'), "id"),
        (turn('outcome = "clarification"\nscale = "fraction_or_percent"'), "comparison options need a reference sql"),
        (turn('outcome = "answered"\nsql = "SELECT 1"\nscale = "percent"'), "scale"),
        (turn('outcome = "answered"\nsql = "SELECT 1"\ndecimal_places = 0'), "decimal_places"),
        (turn('outcome = "clarification"\nor_clarification = true'), "or_clarification needs a reference sql"),
        (turn('outcome = "cannot_answer"\naccept_stopped_write = true'), "accept_stopped_write goes with"),
        (turn('outcome = "answered"\nexpect_truncated = true'), "expect_truncated goes with"),
        (
            turn('outcome = "answered"\nsql = "SELECT 1"\nexpect_truncated = true\nrequire = ["caveat_truncated"]'),
            "a reference sql goes with outcome answered",
        ),
        (
            turn('outcome = "clarification"\nexpect_truncated = true\nrequire = ["caveat_truncated"]'),
            "expect_truncated goes with",
        ),
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


def test_a_rule_turn_can_carry_its_month_table_and_weakest_month(tmp_path: Path) -> None:
    months = 'months = [["2019-03", 3, 2, 1, 1], ["2019-04", 2, 2, 0, 2]]\n'
    text = turn(RULE + months + 'weakest_month = "none"\nrequire = ["caveat_weak_month"]\nforbid = ["caveat_period_drop"]')
    (loaded,) = load_cases(write(tmp_path, text))
    only = loaded.turns[0]
    assert only.weakest_month == "none"
    assert only.expected_months is not None
    assert [row.model_dump() for row in only.expected_months] == [
        {"month": "2019-03", "flagged": 3, "flagged_labelled": 2, "true_positives": 1, "false_positives": 1},
        {"month": "2019-04", "flagged": 2, "flagged_labelled": 2, "true_positives": 0, "false_positives": 2},
    ]
    assert '"weakest_month":"none"' in loaded.model_dump_json()


def test_a_turn_without_the_month_fields_serialises_as_it_did_before_they_existed(tmp_path: Path) -> None:
    # A recorded run keeps a digest of each case's JSON: a case that sets neither
    # field must keep the digest it was recorded with.
    (loaded,) = load_cases(write(tmp_path, turn(RULE)))
    dumped = loaded.model_dump_json()
    assert loaded.turns[0].months is None and loaded.turns[0].expected_months is None
    assert '"months"' not in dumped and '"weakest_month"' not in dumped
    assert '"false_positives":3,"or_rule":null' in dumped


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
    assert len(suite.cases) == 146
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
        "rules": 15,
        "rule_policy": 7,
        "scan": 6,
        "paraphrases": 8,
        "relative_dates": 5,
        "result_edges": 4,
        "conversations": 4,
        "guard_stops": 2,
        "debatable": 3,
    }


def test_every_check_question_is_a_tuned_case() -> None:
    tuned = {case.turns[0].question for case in load_suite().cases if case.tuned}
    assert len(CHECK_QUESTIONS) == 11 and set(CHECK_QUESTIONS) <= tuned


def test_the_cases_the_prompt_was_written_against_are_the_tuned_ones() -> None:
    # The eleven check questions, and eleven whose wording or example is in
    # the prompt text: its three clarify words (in a question, a follow-up and
    # a rule request), "last month" and "this year", the velocity example
    # (twice: "within" and "less than" two minutes), the brief's debit rule as
    # a count, Rome in Georgia, and the wording of the scan's tool.
    # Then the two cases that failed in the first recorded run and had a
    # prompt line written for them afterwards, and the case that asks for the
    # brief's debit rule a second time, for its month table. Then two that
    # came with the scan: the scan tool's own wording with a filter added, and
    # the follow-up that the text said after a scan turn was written for.
    suite = load_suite()
    written_with_the_scan = {
        "scan-not-for-a-filtered-question",
        "scan-then-that-without-naming-it",
    }
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
        "relative-this-year",
        "trap-velocity-window",
        "debatable-velocity-strict",
        "join-brief-wrong-names",
        "trap-city-needs-state",
        "scan-where-is-fraud",
    }
    repeats_a_tuned_rule = {"evidence-brief-rule-by-month"}
    tuned = {case.id for case in suite.cases if case.tuned}
    check = {case.id for case in suite.cases if case.turns[0].question in CHECK_QUESTIONS}
    assert len(check) == 11
    assert tuned == (
        check
        | from_the_prompt
        | fixed_after_the_first_run
        | repeats_a_tuned_rule
        | written_with_the_scan
    )
    assert len(tuned) == 27
    assert suite.case("evidence-brief-rule-by-month").turns[0].clause == (
        suite.case("rule-brief-example").turns[0].clause
    )
    assert "ask which segment they mean" in SCAN_RESULT_UNSEEN
    text = system_text("").lower()
    for word in (
        "risky", "suspicious", "high value", "last month", '"this year"', "within 5 minutes", "'ga'",
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
    # The five cases about the month-by-month evidence, and what each expects.
    by_month = {
        case.id: (turn.weakest_month, len(turn.months or ()), set(turn.require), set(turn.forbid))
        for case in suite.cases
        for turn in case.turns
        if turn.weakest_month is not None or turn.months is not None
    }
    quiet: tuple[set[str], set[str]] = (set(), {"caveat_weak_month", "caveat_period_drop"})
    assert by_month == {
        "evidence-location-by-month": ("2019-03", 10, *quiet),
        "evidence-brief-rule-by-month": ("2019-09", 10, *quiet),
        "evidence-some-months-only": ("2019-07", 10, *quiet),
        "evidence-unlabelled-month": ("2019-06", 10, *quiet),
        "evidence-fading-rule": (
            "2019-08", 10, {"caveat_weak_month", "caveat_period_drop", "caveat_high_flag_rate"}, set(),
        ),
    }  # fmt: skip
    assert all(suite.case(case_id).tier == "rules" for case_id in by_month)
    repeats = {case.id for case in suite.cases if "repeats-a-rule" in case.tags}
    assert repeats == {"evidence-location-by-month", "evidence-brief-rule-by-month"}
    assert suite.case("evidence-location-by-month").turns[0].clause == suite.case("rule-location").turns[0].clause


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
    # One case of the tier is there because it must not use the scan.
    suite = load_suite()
    in_tier = {case.id for case in suite.cases if case.tier == "scan"}
    needing = {case.id for case in suite.cases if case.requires == "scan"}
    assert in_tier - needing == {"scan-not-for-a-filtered-question"} and needing <= in_tier
    assert len(needing) == 5


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


# The options added for the review's cases.


def test_the_later_options_load_and_reach_the_comparison(tmp_path: Path) -> None:
    text = (
        'tier = "debatable"\n[[case]]\nid = "one"\ntests = "t"\ntuned = false\n'
        '[[case.turn]]\nquestion = "q1"\noutcome = "answered"\nsql = "SELECT 0.5"\n'
        'scale = "fraction_or_percent"\ndecimal_places = 2\nor_clarification = true\n'
        'require = ["assumptions_stated"]\n'
        '[[case.turn]]\nquestion = "q2"\noutcome = "refused"\naccept_stopped_write = true\n'
        '[[case.turn]]\nquestion = "q3"\noutcome = "answered"\nexpect_truncated = true\n'
        'require = ["caveat_truncated"]\n'
    )
    first, second, third = load_cases(write(tmp_path, text))[0].turns
    assert (first.comparison.scale, first.comparison.decimal_places) == ("fraction_or_percent", 2)
    assert first.or_clarification and first.require == ("assumptions_stated",)
    assert second.accept_stopped_write and "executed" in second.forbidden
    assert third.expect_truncated and third.sql is None
    plain = load_cases(write(tmp_path / "plain", 'tier = "joins"\n' + ANSWERED.format(id="a")))[0].turns[0]
    assert (plain.comparison.scale, plain.comparison.decimal_places) == ("fraction", None)
    assert not (plain.or_clarification or plain.accept_stopped_write or plain.expect_truncated)


def test_an_option_left_unset_is_not_in_the_serialised_case(tmp_path: Path) -> None:
    # A recorded run keeps a digest of each case as serialised: a case that sets
    # none of the later options must serialise as it did before they existed.
    later = {"scale", "decimal_places", "or_clarification", "accept_stopped_write", "expect_truncated"}
    plain = load_cases(write(tmp_path, 'tier = "joins"\n' + ANSWERED.format(id="a")))[0]
    assert not later & plain.turns[0].model_dump().keys()
    assert not any(name in plain.model_dump_json() for name in later)
    text = turn('outcome = "answered"\nsql = "SELECT 0.5"\ndecimal_places = 2', )
    rounded = load_cases(write(tmp_path / "set", text))[0].turns[0]
    assert later & rounded.model_dump().keys() == {"decimal_places"}


def test_the_cases_added_after_the_review_say_what_they_accept() -> None:
    suite = load_suite()
    turns = {(case.id, number): item for case in suite.cases for number, item in enumerate(case.turns, 1)}
    assert {key[0] for key, item in turns.items() if item.accept_stopped_write} == {
        "conv-follow-up-after-refused-turn", "guard-explain-delete", "guard-rule-clause-escape",
    }  # fmt: skip
    assert {key[0] for key, item in turns.items() if item.scale != "fraction"} == {
        "edges-fraud-share-of-all", "edges-explicit-percentage",
    }  # fmt: skip
    assert {key[0]: item.decimal_places for key, item in turns.items() if item.decimal_places} == {
        "debatable-average-rounded": 2
    }
    assert {key[0] for key, item in turns.items() if item.or_clarification} == {
        "debatable-credit-limit-card-types"
    }
    assert {key[0] for key, item in turns.items() if item.expect_truncated} == {"edges-many-rows"}
    # Every relative date has to say which dates it was resolved to.
    assert all(
        "assumptions_stated" in item.require
        for (case_id, _), item in turns.items()
        if suite.case(case_id).tier == "relative_dates"
    )
