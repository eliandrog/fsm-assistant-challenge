import type { ChatClient } from '../api/client';
import type { Health, Reply, TurnResult } from '../api/types';
import { HEALTH, TURNS } from './fixtures';

/** What a mock replies with: a turn result, a client failure, or a function of the message. */
export type Canned = TurnResult | Reply | ((text: string, conversationId: string) => TurnResult | Reply);

export interface MockOptions {
  /** Replies by the exact message text; anything else gets `fallback`. */
  replies?: Record<string, Canned>;
  fallback?: Canned;
  health?: Health | null;
  /** Simulated latency in ms. */
  latencyMs?: number;
}

export interface MockClient extends ChatClient {
  /** Every message sent, in order. */
  sent: { conversationId: string; text: string }[];
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
}: MockOptions = {}): MockClient {
  const sent: MockClient['sent'] = [];
  return {
    sent,
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
}
