import type { ChatClient } from './client';
import { isHealth, isRecord, isTurnResult } from './guards';
import type { ChatRequest, FailureReason, Reply } from './types';

function failure(reason: FailureReason, message: string): Reply {
  return { kind: 'failure', failure: { reason, message } };
}

const UNREACHABLE =
  'Could not reach the assistant’s server. Check that `fsm-assistant serve` is running, then retry.';

function isAbort(err: unknown): boolean {
  return err instanceof DOMException && err.name === 'AbortError';
}

/** The server's own sentence for a refusal, when the body is its typed error. */
function serverMessage(payload: unknown): string | null {
  return isRecord(payload) && typeof payload.code === 'string' && typeof payload.message === 'string'
    ? payload.message
    : null;
}

export function createHttpClient(base = '/api'): ChatClient {
  return {
    async sendMessage(conversationId, text, options) {
      const body: ChatRequest = { conversation_id: conversationId, message: text };

      let res: Response;
      try {
        res = await fetch(`${base}/chat`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
          body: JSON.stringify(body),
          signal: options?.signal,
        });
      } catch (err) {
        if (isAbort(err)) throw err;
        return failure('network', UNREACHABLE);
      }

      let payload: unknown = null;
      try {
        payload = await res.json();
      } catch (err) {
        if (isAbort(err)) throw err;
        // Not JSON: handled as an unreadable or failed response below.
      }

      if (res.ok) {
        return isTurnResult(payload)
          ? { kind: 'turn', result: payload }
          : failure('unreadable', 'The server sent a reply this interface could not read.');
      }
      const said = serverMessage(payload);
      if (res.status === 409) {
        return failure('busy', said ?? 'This conversation is still answering an earlier question.');
      }
      if (res.status === 422 || res.status === 400) {
        return failure('invalid', said ?? 'The server did not accept the message.');
      }
      // A dev proxy with no backend behind it answers 502 or 504.
      if (res.status === 502 || res.status === 503 || res.status === 504) {
        return failure('network', UNREACHABLE);
      }
      return failure('server', said ?? `The server answered with HTTP ${res.status}.`);
    },

    async health(options) {
      try {
        const res = await fetch(`${base}/health`, {
          headers: { Accept: 'application/json' },
          signal: options?.signal,
        });
        if (!res.ok) return null;
        const payload: unknown = await res.json();
        return isHealth(payload) ? payload : null;
      } catch {
        return null;
      }
    },
  };
}
