/**
 * Real response bodies of the API, written by the backend's test
 * tests/test_web_contract.py, which fails when the API stops returning them.
 */
import type {
  ApiError,
  BankEntry,
  FinderOverview,
  FinderRun,
  Health,
  RunProgress,
  TurnResult,
} from '../api/types';
import apiErrorFinderBusy from './fixtures/api_error_finder_busy.json';
import finderEntryApproved from './fixtures/finder_entry_approved.json';
import finderEntryClarification from './fixtures/finder_entry_clarification.json';
import finderEntryConfirmed from './fixtures/finder_entry_confirmed.json';
import finderEntryDiscarded from './fixtures/finder_entry_discarded.json';
import finderEntryFlagsNothing from './fixtures/finder_entry_flags_nothing.json';
import finderEntryNoSignal from './fixtures/finder_entry_no_signal.json';
import finderEntryNotConfirmed from './fixtures/finder_entry_not_confirmed.json';
import finderEntryProposed from './fixtures/finder_entry_proposed.json';
import finderEntryRejected from './fixtures/finder_entry_rejected.json';
import finderOverview from './fixtures/finder_overview.json';
import finderOverviewEmpty from './fixtures/finder_overview_empty.json';
import finderOverviewNoneConfirmed from './fixtures/finder_overview_none_confirmed.json';
import finderOverviewWaiting from './fixtures/finder_overview_waiting.json';
import finderProgressClarification from './fixtures/finder_progress_clarification.json';
import finderProgressConfirmed from './fixtures/finder_progress_confirmed.json';
import finderProgressDuplicate from './fixtures/finder_progress_duplicate.json';
import finderProgressFailed from './fixtures/finder_progress_failed.json';
import finderProgressNoSignal from './fixtures/finder_progress_no_signal.json';
import finderProgressNotConfirmed from './fixtures/finder_progress_not_confirmed.json';
import finderProgressRejected from './fixtures/finder_progress_rejected.json';
import finderProposed from './fixtures/finder_proposed.json';
import finderProposedDemoExhausted from './fixtures/finder_proposed_demo_exhausted.json';
import finderProposedFailed from './fixtures/finder_proposed_failed.json';
import answeredAfterWrite from './fixtures/answered_after_write.json';
import answeredEmpty from './fixtures/answered_empty.json';
import answeredHostile from './fixtures/answered_hostile.json';
import answeredScalar from './fixtures/answered_scalar.json';
import answeredTable from './fixtures/answered_table.json';
import answeredTruncated from './fixtures/answered_truncated.json';
import apiErrorInvalid from './fixtures/api_error_invalid.json';
import cannotAnswer from './fixtures/cannot_answer.json';
import clarification from './fixtures/clarification.json';
import failedModel from './fixtures/failed_model.json';
import failedSql from './fixtures/failed_sql.json';
import healthDemo from './fixtures/health_demo.json';
import healthLive from './fixtures/health_live.json';
import healthNoDatabase from './fixtures/health_no_database.json';
import healthNoKey from './fixtures/health_no_key.json';
import rejected from './fixtures/rejected.json';
import rejectedWrite from './fixtures/rejected_write.json';
import ruleProposed from './fixtures/rule_proposed.json';
import scan from './fixtures/scan.json';

// JSON modules type a literal as `string`; the backend's test vouches for the shapes.
const turn = (body: unknown) => body as TurnResult;
const health = (body: unknown) => body as Health;

export const TURNS = {
  answeredScalar: turn(answeredScalar),
  answeredTable: turn(answeredTable),
  answeredTruncated: turn(answeredTruncated),
  answeredEmpty: turn(answeredEmpty),
  answeredHostile: turn(answeredHostile),
  ruleProposed: turn(ruleProposed),
  scan: turn(scan),
  clarification: turn(clarification),
  cannotAnswer: turn(cannotAnswer),
  rejected: turn(rejected),
  rejectedWrite: turn(rejectedWrite),
  answeredAfterWrite: turn(answeredAfterWrite),
  failedSql: turn(failedSql),
  failedModel: turn(failedModel),
};

export const HEALTH = {
  live: health(healthLive),
  noKey: health(healthNoKey),
  demo: health(healthDemo),
  noDatabase: health(healthNoDatabase),
};

export const API_ERROR_INVALID = apiErrorInvalid as ApiError;
export const API_ERROR_FINDER_BUSY = apiErrorFinderBusy as ApiError;

const frames = (body: unknown) => body as RunProgress[];

export const FINDER = {
  /** After everything below: five processed entries, one scenario waiting, one discarded. */
  overview: finderOverview as unknown as FinderOverview,
  overviewEmpty: finderOverviewEmpty as unknown as FinderOverview,
  overviewNoneConfirmed: finderOverviewNoneConfirmed as unknown as FinderOverview,
  /** Eight scenarios proposed and none processed yet. */
  overviewWaiting: finderOverviewWaiting as unknown as FinderOverview,
  proposed: finderProposed as unknown as FinderRun,
  proposedFailed: finderProposedFailed as unknown as FinderRun,
  proposedDemoExhausted: finderProposedDemoExhausted as unknown as FinderRun,
  /** Every progress body of processing one scenario, in the order the page would poll them. */
  progress: {
    confirmed: frames(finderProgressConfirmed),
    notConfirmed: frames(finderProgressNotConfirmed),
    noSignal: frames(finderProgressNoSignal),
    clarification: frames(finderProgressClarification),
    rejected: frames(finderProgressRejected),
    duplicate: frames(finderProgressDuplicate),
    failed: frames(finderProgressFailed),
  },
  entries: {
    confirmed: finderEntryConfirmed as unknown as BankEntry,
    approved: finderEntryApproved as unknown as BankEntry,
    notConfirmed: finderEntryNotConfirmed as unknown as BankEntry,
    noSignal: finderEntryNoSignal as unknown as BankEntry,
    clarification: finderEntryClarification as unknown as BankEntry,
    rejected: finderEntryRejected as unknown as BankEntry,
    proposed: finderEntryProposed as unknown as BankEntry,
    discarded: finderEntryDiscarded as unknown as BankEntry,
    /** Entry 4 of the bank of `overviewNoneConfirmed`: a long one-line clause that flags nothing. */
    flagsNothing: finderEntryFlagsNothing as unknown as BankEntry,
  },
};
