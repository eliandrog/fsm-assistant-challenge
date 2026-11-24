/**
 * The API's bodies, as the interface names them. The shapes themselves are in
 * types.gen.ts, which is generated from the backend's models: change a shape
 * there (see that file's header), not here.
 */
import type { Column, TurnError, TurnResult } from './types.gen';

export type {
  ApiError,
  Caveat,
  Cell,
  ChatRequest,
  Column,
  Health,
  RuleCard,
  RuleMonth,
  RulePeriod,
  TableResult,
  TurnError,
  TurnMeta,
  TurnResult,
} from './types.gen';

export type Status = TurnResult['status'];
export type ErrorKind = TurnError['kind'];
export type ColumnFormat = Column['format'];

/**
 * Why no turn result came back: the request never got an answer, or the
 * server refused it. Made by the client; the backend never sends this.
 */
export type FailureReason = 'network' | 'busy' | 'invalid' | 'server' | 'unreadable';

export interface ClientFailure {
  reason: FailureReason;
  /** Safe to show: written by this interface or by the server's code. */
  message: string;
}

/** What one question got back. */
export type Reply = { kind: 'turn'; result: TurnResult } | { kind: 'failure'; failure: ClientFailure };
