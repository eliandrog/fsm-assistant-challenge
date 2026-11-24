"""Aggregation and rendering of a run, on hand-built results."""

import json
import re

from fsm_assistant.evals.cases import Case, Tier, Turn, load_suite
from fsm_assistant.evals.report import (
    PRICES_USD_PER_MILLION,
    CaseResult,
    RepeatSummary,
    RunInfo,
    Summary,
    Tally,
    TurnScore,
    case_result,
    operations,
    render_json,
    render_markdown,
    render_repeat_markdown,
    skip_reason,
    summarise,
    summarise_repeats,
)
from fsm_assistant.evals.scorers import FailureReason, Note, Score

INFO = RunInfo(
    model_id="claude-test",
    effort="medium",
    tool_mode="tools",
    prompt_version="prompt-1",
    source="replay",
    schema_context_sha256="a" * 64,
    suite_sha256="b" * 64,
)
OK = Score(scorer="outcome", passed=True, detail="answered, as expected")
MATCH = Score(scorer="result", passed=True, detail="1 rows match the reference")
PERCENT = Score(
    scorer="result", passed=False, reason=FailureReason.SCALE_MISMATCH, detail="a fraction | as a percentage"
)
WIDER = Score(
    scorer="result", passed=True, note=Note.EXTRA_COLUMNS, detail="1 rows match; 2 further column(s) follow"
)
ASKED = Score(scorer="outcome", passed=False, reason=FailureReason.NOT_ANSWERED, detail="expected answered")


def result(
    case_id: str,
    tier: Tier,
    *scores: Score,
    tuned: bool = False,
    tags: tuple[str, ...] = (),
    paraphrase_of: str | None = None,
    skipped: str | None = None,
    known_limitation: str | None = None,
) -> CaseResult:
    return CaseResult(
        case_id=case_id,
        tier=tier,
        tags=tags,
        tuned=tuned,
        paraphrase_of=paraphrase_of,
        known_limitation=known_limitation,
        skipped=skipped,
        turns=(TurnScore(turn=1, scores=scores),) if scores else (),
    )


RESULTS = (
    result("total", "aggregates", OK, MATCH, tuned=True, tags=("count",)),
    result("rate", "fraud", OK, PERCENT, tags=("rate", "labels")),
    result("coverage", "fraud", OK, MATCH, tags=("labels",)),
    result("rate-reworded", "paraphrases", ASKED, PERCENT, tags=("rate",), paraphrase_of="rate"),
    result("coverage-reworded", "paraphrases", OK, MATCH, paraphrase_of="coverage"),
    result("orphan-reworded", "paraphrases", OK, paraphrase_of="not-run"),
    result("scan", "scan", skipped="needs scan, which is not built"),
)


def tally(summary: Summary, group: str, name: str) -> Tally:
    groups: dict[str, tuple[Tally, ...]] = {
        "by_tier": summary.by_tier,
        "by_split": summary.by_split,
        "by_tag": summary.by_tag,
        "by_scorer": summary.by_scorer,
    }
    found = [item for item in groups[group] if item.name == name]
    assert len(found) == 1, (group, name)
    return found[0]


def counts(item: Tally) -> tuple[int, int, int, int]:
    return item.cases, item.passed, item.failed, item.skipped


def test_a_case_passes_only_when_every_score_of_every_turn_passed() -> None:
    assert result("a", "fraud", OK, MATCH).passed
    assert not result("a", "fraud", OK, PERCENT).passed
    assert not result("a", "scan", skipped="needs scan").passed
    assert not result("a", "fraud").passed  # nothing scored is not a pass
    two_turns = CaseResult(
        case_id="a", tier="follow_ups", tags=(), tuned=False, paraphrase_of=None,
        turns=(TurnScore(turn=1, scores=(OK,)), TurnScore(turn=2, scores=(ASKED,))),
    )
    assert not two_turns.passed


def test_counts_per_tier_split_tag_and_scorer() -> None:
    summary = summarise(RESULTS, INFO)
    assert counts(summary.overall) == (7, 4, 2, 1)
    assert [item.name for item in summary.by_tier] == ["aggregates", "fraud", "scan", "paraphrases"]
    assert counts(tally(summary, "by_tier", "fraud")) == (2, 1, 1, 0)
    assert counts(tally(summary, "by_tier", "scan")) == (1, 0, 0, 1)
    assert counts(tally(summary, "by_tier", "paraphrases")) == (3, 2, 1, 0)
    assert counts(tally(summary, "by_split", "tuned")) == (1, 1, 0, 0)
    assert counts(tally(summary, "by_split", "held_out")) == (6, 3, 2, 1)
    assert [item.name for item in summary.by_tag] == ["count", "labels", "rate"]
    assert counts(tally(summary, "by_tag", "rate")) == (2, 0, 2, 0)
    assert counts(tally(summary, "by_tag", "labels")) == (2, 1, 1, 0)
    assert counts(tally(summary, "by_scorer", "result")) == (5, 3, 2, 0)
    assert counts(tally(summary, "by_scorer", "outcome")) == (6, 5, 1, 0)


MARKED = (
    result("wide", "fraud", OK, WIDER, tags=("rate",)),
    result("wide-and-wrong", "fraud", ASKED, WIDER),
    result("limited", "follow_ups", OK, ASKED, known_limitation="A declined turn | is not remembered"),
    result("fixed-since", "follow_ups", OK, MATCH, known_limitation="Was a limitation"),
    result("limited-scan", "scan", skipped="needs scan", known_limitation="Not built"),
    result("plain", "fraud", OK, PERCENT),
)


def marks(item: Tally) -> tuple[int, int]:
    return item.with_extra_columns, item.known_limitation


def test_extra_columns_and_known_limitations_are_counted_inside_passed_and_failed() -> None:
    summary = summarise(MARKED, INFO)
    assert counts(summary.overall) == (6, 2, 3, 1)  # a known limitation is a failure like any other
    assert marks(summary.overall) == (1, 1)
    # A case that failed elsewhere is not "correct, with extra columns".
    assert counts(tally(summary, "by_tier", "fraud")) == (3, 1, 2, 0)
    assert marks(tally(summary, "by_tier", "fraud")) == (1, 0)
    assert counts(tally(summary, "by_tier", "follow_ups")) == (2, 1, 1, 0)
    assert marks(tally(summary, "by_tier", "follow_ups")) == (0, 1)
    assert marks(tally(summary, "by_tier", "scan")) == (0, 0)  # skipped: neither passed nor failed
    assert marks(tally(summary, "by_split", "held_out")) == (1, 1)
    assert marks(tally(summary, "by_tag", "rate")) == (1, 0)
    assert tally(summary, "by_scorer", "result").with_extra_columns == 2  # counted in scores
    assert [(item.case_id, item.passed, item.reason) for item in summary.known_limitations] == [
        ("limited", False, "A declined turn | is not remembered"),
        ("fixed-since", True, "Was a limitation"),
    ]
    assert {failure.case_id for failure in summary.failures} == {"wide-and-wrong", "limited", "plain"}


def test_the_markdown_shows_both_counts_and_a_known_limitation_that_passed() -> None:
    text = render_markdown(summarise(MARKED, INFO))
    assert "**2 of 5 cases passed** (6 cases in the suite, 1 skipped)" in text
    assert "- Correct, with extra columns: 1 (counted in the 2 passed)" in text
    assert "- Failed, known limitation: 1 (counted in the 3 failed)" in text
    assert "- Marked as a known limitation but passed, so the note is out of date: fixed-since" in text
    assert (
        "| Tier | Cases | Passed | With extra columns | Stopped by guard | Failed "
        "| Known limitation | Skipped |"
    ) in text
    assert "| fraud | 3 | 1 | 1 | 0 | 2 | 0 | 0 |" in text
    assert "| follow_ups | 2 | 1 | 0 | 0 | 1 | 1 | 0 |" in text
    assert "| limited | failed, as noted | A declined turn \\| is not remembered |" in text
    assert "| fixed-since | passed: the note is out of date | Was a limitation |" in text
    assert "limited-scan" not in text

    plain = render_markdown(summarise(RESULTS, INFO))
    assert "- Correct, with extra columns: 0 (counted in the 4 passed)" in plain
    assert "- Failed, known limitation: 0 (counted in the 2 failed)" in plain
    assert "the note is out of date" not in plain
    assert "No case marked as a known limitation was run." in plain


def test_failures_reasons_and_paraphrase_pairs() -> None:
    summary = summarise(RESULTS, INFO)
    assert [(item.case_id, item.turn, item.scorer, item.reason) for item in summary.failures] == [
        ("rate", 1, "result", FailureReason.SCALE_MISMATCH),
        ("rate-reworded", 1, "outcome", FailureReason.NOT_ANSWERED),
        ("rate-reworded", 1, "result", FailureReason.SCALE_MISMATCH),
    ]
    assert [(item.scorer, item.reason, item.count) for item in summary.reasons] == [
        ("result", FailureReason.SCALE_MISMATCH, 2),
        ("outcome", FailureReason.NOT_ANSWERED, 1),
    ]
    assert [
        (pair.original, pair.paraphrase, pair.original_passed, pair.paraphrase_passed)
        for pair in summary.paraphrases
    ] == [
        ("rate", "rate-reworded", False, False),
        ("coverage", "coverage-reworded", True, True),
        ("not-run", "orphan-reworded", None, True),
    ]


def test_an_empty_run_summarises() -> None:
    summary = summarise((), INFO)
    assert counts(summary.overall) == (0, 0, 0, 0)
    assert summary.by_tier == () and summary.failures == ()
    text = render_markdown(summary)
    assert "**0 of 0 cases passed**" in text
    assert "No score failed." in text and "No paraphrase case was run." in text


def test_the_markdown_report() -> None:
    text = render_markdown(summarise(RESULTS, INFO))
    headline = text.index("**4 of 6 cases passed** (7 cases in the suite, 1 skipped)")
    assert headline < text.index("## By tier") < text.index("| fraud | 2 | 1 | 0 | 0 | 1 | 0 | 0 |")
    assert "| held_out | 6 | 3 | 0 | 0 | 2 | 0 | 1 |" in text
    assert "| result | scale_mismatch | 2 |" in text
    assert "| rate | fail | rate-reworded | fail |" in text
    assert "| not-run | not run | orphan-reworded | pass |" in text
    assert "| rate | 1 | result | scale_mismatch | a fraction \\| as a percentage |" in text
    assert "`claude-test`" in text and "recorded replies, replayed" in text
    assert "`aaaaaaaaaaaa`" in text and "a" * 13 not in text
    assert "%" not in text  # counts with their sample size, never a percentage
    assert text.endswith("\n") and "\n\n\n" not in text


def test_reports_carry_no_time_and_no_path() -> None:
    summary = summarise(RESULTS, INFO)
    for text in (render_markdown(summary), render_json(summary)):
        assert not re.search(r"\d{4}-\d{2}-\d{2}|\d{2}:\d{2}:\d{2}", text)
        assert "/" not in text.replace("\\|", "")
    assert render_markdown(summary) == render_markdown(summarise(RESULTS, INFO))


def test_the_json_report_round_trips() -> None:
    summary = summarise(RESULTS, INFO)
    text = render_json(summary)
    assert Summary.model_validate_json(text) == summary
    document = json.loads(text)
    assert document["overall"] == {
        "name": "all", "cases": 7, "passed": 4, "failed": 2, "skipped": 1,
        "with_extra_columns": 0, "stopped_by_guard": 0, "known_limitation": 0,
    }  # fmt: skip
    assert document["operations"]["turns"] == 6 and document["operations"]["cost"] is None
    assert document["known_limitations"] == []
    assert document["failures"][0]["reason"] == "scale_mismatch"
    assert document["info"]["model_id"] == "claude-test"


def test_cases_that_need_an_unbuilt_feature_are_skipped() -> None:
    scan = Case(
        id="scan", tier="scan", tests="t", tuned=False, requires="scan",
        turns=(Turn(question="Where?", outcome="answered", action="profile_fraud"),),
    )
    plain = Case(
        id="plain", tier="ambiguous", tests="t", tuned=True, tags=("vague-term",),
        turns=(Turn(question="Risky?", outcome="clarification"),),
    )
    assert skip_reason(scan, frozenset()) == "needs scan, which is not built"
    assert skip_reason(scan, frozenset({"scan"})) is None
    assert skip_reason(plain, frozenset()) is None

    skipped = case_result(scan, skipped=skip_reason(scan, frozenset()))
    assert skipped.skipped is not None and not skipped.passed and skipped.turns == ()
    scored = case_result(plain, [TurnScore(turn=1, scores=(OK,))])
    assert scored.passed and scored.tuned and scored.tags == ("vague-term",)
    assert scored.known_limitation is None
    marked = case_result(plain.model_copy(update={"known_limitation": "Why"}), [TurnScore(turn=1, scores=(ASKED,))])
    assert marked.known_limitation == "Why" and marked.failed


def test_a_run_of_the_whole_suite_keeps_every_case_in_the_table() -> None:
    suite = load_suite()
    results = [
        case_result(case, skipped=reason)
        if (reason := skip_reason(case, frozenset())) is not None
        else case_result(case, [TurnScore(turn=number, scores=(OK,)) for number in range(1, len(case.turns) + 1)])
        for case in suite.cases
    ]
    summary = summarise(results, INFO)
    assert counts(summary.overall) == (146, 141, 0, 5)
    assert sum(item.cases for item in summary.by_tier) == 146
    assert sum(item.cases for item in summary.by_split) == 146
    assert counts(tally(summary, "by_split", "tuned")) == (27, 25, 0, 2)
    assert [item.case_id for item in summary.known_limitations] == ["follow-after-decline"]
    assert summary.known_limitations[0].passed
    assert len(summary.paraphrases) == 8
    assert all(pair.original_passed and pair.paraphrase_passed for pair in summary.paraphrases)


# A pass the guard took part in, and what the turns took.

GUARDED = Score(
    scorer="guardrail", passed=True, note=Note.STOPPED_BY_GUARD,
    detail="0 executed, all accepted by the guard; the guard refused 1 reply(ies) of this turn",
)  # fmt: skip


def taken(
    case_id: str,
    tier: Tier,
    *scores: Score,
    calls: int = 1,
    refused: tuple[str, ...] = (),
    writes: int = 0,
    tokens: tuple[int, int] = (4_600, 350),
    seconds: float = 2.0,
    tuned: bool = False,
) -> CaseResult:
    return CaseResult(
        case_id=case_id, tier=tier, tags=(), tuned=tuned, paraphrase_of=None,
        turns=(
            TurnScore(
                turn=1, scores=scores, model_calls=calls, refused=refused, write_attempts=writes,
                input_tokens=tokens[0], output_tokens=tokens[1], seconds=seconds,
            ),
        ),
    )  # fmt: skip


TAKEN = (
    taken("clean", "aggregates", OK, MATCH, seconds=1.0, tuned=True),
    taken("repaired", "guard_stops", OK, GUARDED, calls=2, refused=("sqlite_error",),
          tokens=(9_200, 700), seconds=6.0),
    taken("stopped", "guard_stops", OK, GUARDED, calls=2, refused=("explain", "explain"), writes=2,
          tokens=(9_200, 700), seconds=4.0),
    taken("reshaped", "aggregates", OK, MATCH, calls=2, refused=("no_tool_call",), seconds=3.0),
    taken("guarded-and-wrong", "guard_stops", ASKED, GUARDED, refused=("explain",), writes=1),
)  # fmt: skip


def test_a_pass_the_guard_took_part_in_is_counted_apart() -> None:
    summary = summarise(TAKEN, INFO)
    assert counts(summary.overall) == (5, 4, 1, 0)
    assert summary.overall.stopped_by_guard == 2  # a failed case is not a pass of any kind
    assert tally(summary, "by_tier", "guard_stops").stopped_by_guard == 2
    assert tally(summary, "by_tier", "aggregates").stopped_by_guard == 0
    assert tally(summary, "by_split", "held_out").stopped_by_guard == 2
    assert tally(summary, "by_scorer", "guardrail").stopped_by_guard == 3  # counted in scores
    text = render_markdown(summary)
    assert "- Passed after the guard refused a reply: 2 (counted in the 4 passed)" in text
    assert "| guard_stops | 3 | 2 | 0 | 2 | 1 | 0 | 0 |" in text


def test_repairs_refusals_seconds_tokens_and_cost() -> None:
    item = operations(TAKEN, "claude-haiku-5-5")
    assert (item.turns, item.model_requests, item.repairs, item.write_attempts) == (5, 8, 3, 3)
    assert [(entry.code, entry.by, entry.count) for entry in item.refusals] == [
        ("explain", "guard", 3),
        ("no_tool_call", "shape_check", 1),
        ("sqlite_error", "guard", 1),
    ]
    # Nearest rank over 1, 2, 3, 4, 6 seconds.
    assert (item.seconds_p50, item.seconds_p95, item.seconds_max) == (3.0, 6.0, 6.0)
    assert (item.input_tokens, item.output_tokens) == (32_200, 2_450)
    assert (item.input_tokens_per_turn, item.output_tokens_per_turn) == (6_440.0, 490.0)
    assert PRICES_USD_PER_MILLION["claude-haiku-5-5"] == (0.10, 0.50)
    assert item.cost is not None
    assert item.cost.run_usd == round((32_200 * 0.10 + 2_450 * 0.50) / 1_000_000, 6) == 0.004445
    assert item.cost.per_turn_usd == 0.000889
    opus = operations(TAKEN, "claude-opus-5-5").cost
    assert opus is not None and opus.run_usd == round((32_200 * 4 + 2_450 * 20) / 1_000_000, 6)
    assert operations(TAKEN, "some-other-model").cost is None

    nothing = operations((), "claude-haiku-5-5")
    assert nothing.seconds_p50 is None and nothing.input_tokens_per_turn is None
    assert nothing.cost is not None and nothing.cost.per_turn_usd is None


def test_the_report_states_what_the_turns_took_and_that_prices_are_constants() -> None:
    summary = summarise(TAKEN, INFO.model_copy(update={"model_id": "claude-haiku-5-5"}))
    text = render_markdown(summary)
    assert "## Repairs, refusals and cost" in text
    assert "- Repairs (turns that needed a second model request): 3 of 5 turns; 8 model requests." in text
    assert "- Refused replies by code: explain 3, no_tool_call 1, sqlite_error 1." in text
    assert "- Write attempts stopped by the guard: 3." in text
    assert "- Seconds per turn: p50 3.00, p95 6.00, max 6.00." in text
    assert "- Tokens: 32,200 in and 2,450 out; per turn 6,440 in and 490 out." in text
    assert "- Estimated cost: $0.0044 for these turns, $0.00089 per question" in text
    assert "constants written in the code when it was built" in text and "may be out of date" in text
    document = json.loads(render_json(summary))
    assert document["operations"]["repairs"] == 3
    assert document["operations"]["cost"]["input_usd_per_million"] == 0.1
    unknown = render_markdown(summarise(TAKEN, INFO))
    assert "no price is written in the code for this model" in unknown


# A repeated run.


def test_a_repeated_run_counts_per_case_how_many_repetitions_passed() -> None:
    always = taken("always", "aggregates", OK, MATCH, tuned=True)
    flaky_pass, flaky_fail = taken("flaky", "fraud", OK, MATCH), taken("flaky", "fraud", OK, PERCENT)
    never = taken("never", "fraud", ASKED)
    skipped = result("scan", "scan", skipped="needs scan")
    partial = taken("only-twice", "fraud", OK, MATCH)
    summary = summarise_repeats(
        [
            [always, flaky_pass, never, skipped, partial],
            [always, flaky_fail, never, skipped, partial],
            [always, flaky_pass, never, skipped],
        ],
        INFO,
    )
    assert summary.repetitions == 3
    # A case skipped, or missing from a repetition, is not counted.
    assert [(case.case_id, case.passed, case.repetitions) for case in summary.cases] == [
        ("always", 3, 3), ("flaky", 2, 3), ("never", 0, 3),
    ]  # fmt: skip
    overall = summary.overall
    assert (overall.cases, overall.passed_all, overall.passed_some, overall.passed_none) == (3, 1, 1, 1)
    fraud = next(item for item in summary.by_tier if item.name == "fraud")
    assert (fraud.cases, fraud.passed_all, fraud.passed_some, fraud.passed_none) == (2, 0, 1, 1)
    held_out = next(item for item in summary.by_split if item.name == "held_out")
    assert (held_out.cases, held_out.passed_all) == (2, 0)
    assert summary.operations.turns == 9  # every repetition's turns

    text = render_repeat_markdown(summary)
    assert "**1 of 3 cases passed all 3 repetitions**; 1 passed some and 1 passed none." in text
    assert "| Tier | Cases | Passed all 3 | Passed some | Passed none |" in text
    assert "| fraud | 2 | 0 | 1 | 1 |" in text
    assert "| flaky | fraud | 2 of 3 |" in text and "| never | fraud | 0 of 3 |" in text
    assert "not a rate" in text and "%" not in text
    assert not re.search(r"\d{4}-\d{2}-\d{2}|\d{2}:\d{2}:\d{2}", text)
    assert RepeatSummary.model_validate_json(render_json(summary)) == summary

    steady = summarise_repeats([[always], [always]], INFO)
    assert "None." in render_repeat_markdown(steady)
    assert summarise_repeats([], INFO).cases == ()
