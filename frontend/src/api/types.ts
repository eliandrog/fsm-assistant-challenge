/**
 * The API's bodies, as the interface names them. The shapes themselves are in
 * types.gen.ts, which is generated from the backend's models: change a shape
 * there (see that file's header), not here.
 */
import type { Column, DecisionRequest, EntrySummary, RunProgress, TurnError, TurnResult } from './types.gen';

export type {
  ApiError,
  BankEntry,
  Caveat,
  Cell,
  ChatRequest,
  Column,
  Criteria,
  Criterion,
  DecisionRequest,
  EntrySummary,
  FinderOverview,
  FinderRun,
  Health,
  ProposedScenario,
  ProposeRequest,
  RuleCard,
  RuleMonth,
  RulePeriod,
  RunItem,
  RunProgress,
  ScenarioMatch,
  TableResult,
  TurnError,
  TurnMeta,
  TurnResult,
} from './types.gen';

export type Status = TurnResult['status'];
export type ErrorKind = TurnError['kind'];
export type ColumnFormat = Column['format'];
export type EvidenceStatus = EntrySummary['status'];
export type Decision = DecisionRequest['decision'];
export type StepName = NonNullable<RunProgress['step']>;

/**
 * Why no turn result came back: the request never got an answer, or the
 * server refused it. Made by the client; the backend never sends this.
 */
export type FailureReason = 'network' | 'busy' | 'invalid' | 'server' | 'unreadable' | 'missing';

export interface ClientFailure {
  reason: FailureReason;
  /** Safe to show: written by this interface or by the server's code. */
  message: string;
}

/** What a rule-finder request got back: the body asked for, or why there is none. */
export type Outcome<T> = { kind: 'ok'; value: T } | { kind: 'failure'; failure: ClientFailure };

/** What one question got back. */
export type Reply = { kind: 'turn'; result: TurnResult } | { kind: 'failure'; failure: ClientFailure };
