import { createHttpClient } from './httpClient';
import type {
  BankEntry,
  Decision,
  FinderOverview,
  FinderRun,
  Health,
  Outcome,
  Reply,
  RunProgress,
} from './types';

export interface SendOptions {
  /** Aborting rejects the promise with an AbortError. */
  signal?: AbortSignal;
}

/**
 * The only seam between the UI and the backend. `sendMessage` resolves with a
 * Reply for every outcome, a failure to reach the server included, and
 * rejects only when aborted. `health` resolves with null when the server
 * cannot be reached or answers with something else.
 */
export interface ChatClient {
  sendMessage(conversationId: string, text: string, options?: SendOptions): Promise<Reply>;
  health(options?: SendOptions): Promise<Health | null>;
  /** The rule finder. Each resolves with an Outcome for every answer, a failed request included. */
  finderOverview(): Promise<Outcome<FinderOverview>>;
  finderEntry(id: number): Promise<Outcome<BankEntry>>;
  /** One model request: a scenario saved as proposed. No SQL is written or run. */
  finderPropose(): Promise<Outcome<FinderRun>>;
  /** Starts turning a proposed scenario into a rule; resolves at once with the first progress. */
  finderProcess(id: number): Promise<Outcome<RunProgress>>;
  /** Where a processing run stands. A failure of reason `missing`: the server no longer knows it. */
  finderProgress(runId: number): Promise<Outcome<RunProgress>>;
  finderDiscard(id: number): Promise<Outcome<BankEntry>>;
  /** Removes an entry from the rule bank for good. */
  finderDelete(id: number): Promise<Outcome<null>>;
  finderDecide(id: number, decision: Decision, note: string): Promise<Outcome<BankEntry>>;
}

/** The real API, at the page's own origin. Tests pass a client of their own to <App>. */
export function createClient(): ChatClient {
  return createHttpClient();
}
