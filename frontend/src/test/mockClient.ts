import type { ChatClient } from '../api/client';
import type {
  BankEntry,
  ClientFailure,
  Decision,
  FinderOverview,
  FinderRun,
  Health,
  Reply,
  RunProgress,
  TurnResult,
} from '../api/types';
import { FINDER, HEALTH, TURNS } from './fixtures';

/** What a mock replies with: a turn result, a client failure, or a function of the message. */
export type Canned = TurnResult | Reply | ((text: string, conversationId: string) => TurnResult | Reply);

export interface MockOptions {
  /** Replies by the exact message text; anything else gets `fallback`. */
  replies?: Record<string, Canned>;
  fallback?: Canned;
  health?: Health | null;
  /** Simulated latency in ms. */
  latencyMs?: number;
  /** The rule bank the finder starts with, and what its actions answer. */
  finder?: {
    overview?: FinderOverview;
    entries?: BankEntry[];
    /** What "Find a rule" answers, and the bank afterwards. */
    propose?: FinderRun | ClientFailure;
    afterPropose?: FinderOverview;
    /** The progress bodies of a processing run, handed out one per request; the last one repeats. */
    frames?: RunProgress[];
    process?: ClientFailure;
    afterProcess?: FinderOverview;
    /** Progress requests wait for this, to catch the page mid-run. */
    hold?: Promise<void>;
  };
}

export interface MockClient extends ChatClient {
  /** Every message sent, in order. */
  sent: { conversationId: string; text: string }[];
  /** Every finder action sent, in order. */
  decisions: { id: number; decision: Decision; note: string }[];
  calls: string[];
}

function wait(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException('Aborted', 'AbortError'));
      return;
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    function onAbort() {
      clearTimeout(timer);
      reject(new DOMException('Aborted', 'AbortError'));
    }
    signal?.addEventListener('abort', onAbort, { once: true });
  });
}

function asReply(value: TurnResult | Reply): Reply {
  return 'kind' in value ? value : { kind: 'turn', result: value };
}

/** A client for tests: answers from the backend's fixtures, with no server. */
export function createMockClient({
  replies = {},
  fallback = TURNS.answeredScalar,
  health = HEALTH.live,
  latencyMs = 0,
  finder = {},
}: MockOptions = {}): MockClient {
  const sent: MockClient['sent'] = [];
  const decisions: MockClient['decisions'] = [];
  const calls: string[] = [];
  let overview = finder.overview ?? FINDER.overview;
  // `approved` and `proposed` are entry 1 again, at other moments, and `discarded` entry 8.
  const later: BankEntry[] = [
    FINDER.entries.approved,
    FINDER.entries.discarded,
    FINDER.entries.proposed,
    FINDER.entries.flagsNothing,
  ];
  const asFound = Object.values(FINDER.entries).filter((entry) => !later.includes(entry));
  const entries = new Map((finder.entries ?? asFound).map((entry) => [entry.id, entry]));
  const missing: ClientFailure = { reason: 'missing', message: 'The rule bank has no such entry.' };
  const queue = [...(finder.frames ?? FINDER.progress.confirmed)];
  const without = (id: number): FinderOverview => ({
    ...overview,
    proposed: overview.proposed.filter((scenario) => scenario.id !== id),
    entries: overview.entries.filter((entry) => entry.id !== id),
  });
  const client: MockClient = {
    sent,
    decisions,
    calls,
    async finderOverview() {
      return { kind: 'ok', value: structuredClone(overview) };
    },
    async finderEntry(id) {
      const entry = entries.get(id);
      return entry ? { kind: 'ok', value: structuredClone(entry) } : { kind: 'failure', failure: missing };
    },
    async finderPropose() {
      calls.push('propose');
      const run = finder.propose ?? FINDER.proposed;
      if ('reason' in run) return { kind: 'failure', failure: run };
      overview = finder.afterPropose ?? overview;
      return { kind: 'ok', value: structuredClone(run) };
    },
    async finderProcess(id) {
      calls.push(`process ${id}`);
      if (finder.process) return { kind: 'failure', failure: finder.process };
      return { kind: 'ok', value: structuredClone(queue[0]!) };
    },
    async finderProgress(runId) {
      calls.push(`progress ${runId}`);
      await finder.hold;
      if (queue.length > 1) queue.shift();
      const frame = queue[0]!;
      if (frame.state !== 'running') overview = finder.afterProcess ?? overview;
      return { kind: 'ok', value: structuredClone(frame) };
    },
    async finderDiscard(id) {
      calls.push(`discard ${id}`);
      overview = { ...without(id), discarded: overview.discarded + 1 };
      return { kind: 'ok', value: structuredClone(FINDER.entries.discarded) };
    },
    async finderDelete(id) {
      calls.push(`delete ${id}`);
      overview = without(id);
      entries.delete(id);
      return { kind: 'ok', value: null };
    },
    async finderDecide(id, decision, note) {
      decisions.push({ id, decision, note });
      const entry = entries.get(id);
      if (!entry) return { kind: 'failure', failure: missing };
      const decided: BankEntry = {
        ...entry,
        decision,
        decision_note: note.trim() || null,
        decided_at: decision === 'pending' ? null : '2000-01-01T00:00:00+00:00',
      };
      entries.set(id, decided);
      overview = {
        ...overview,
        entries: overview.entries.map((row) => (row.id === id ? { ...row, decision } : row)),
      };
      return { kind: 'ok', value: structuredClone(decided) };
    },
    async sendMessage(conversationId, text, options) {
      sent.push({ conversationId, text });
      await wait(latencyMs, options?.signal);
      const canned = replies[text] ?? fallback;
      return structuredClone(asReply(typeof canned === 'function' ? canned(text, conversationId) : canned));
    },
    async health() {
      return health;
    },
  };
  return client;
}
