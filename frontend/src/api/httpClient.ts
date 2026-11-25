import type { ChatClient } from './client';
import {
  isBankEntry,
  isFinderOverview,
  isFinderRun,
  isHealth,
  isRecord,
  isRunProgress,
  isTurnResult,
} from './guards';
import type { ChatRequest, ClientFailure, DecisionRequest, FailureReason, Outcome, Reply } from './types';

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

/** One rule-finder request: the body when it is the shape asked for, a failure otherwise. */
async function finderRequest<T>(
  url: string,
  check: (value: unknown) => value is T,
  body?: unknown,
  method = body === undefined ? 'GET' : 'POST',
): Promise<Outcome<T>> {
  const fail = (reason: FailureReason, message: string): Outcome<T> => {
    const failure: ClientFailure = { reason, message };
    return { kind: 'failure', failure };
  };
  let res: Response;
  try {
    res = await fetch(url, {
      method,
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    return fail('network', UNREACHABLE);
  }
  let payload: unknown = null;
  try {
    payload = await res.json();
  } catch {
    // Not JSON: handled as an unreadable or failed response below.
  }
  if (res.ok) {
    return check(payload)
      ? { kind: 'ok', value: payload }
      : fail('unreadable', 'The server sent a reply this interface could not read.');
  }
  const said = serverMessage(payload);
  if (res.status === 404) return fail('missing', said ?? 'The server has nothing at that address.');
  if (res.status === 409) return fail('busy', said ?? 'The rule finder is busy.');
  if (res.status === 422 || res.status === 400) return fail('invalid', said ?? 'The server did not accept the request.');
  if (res.status === 502 || res.status === 504) return fail('network', UNREACHABLE);
  return fail('server', said ?? `The server answered with HTTP ${res.status}.`);
}

export function createHttpClient(base = '/api'): ChatClient {
  return {
    finderOverview: () => finderRequest(`${base}/finder/entries`, isFinderOverview),
    finderEntry: (id) => finderRequest(`${base}/finder/entries/${id}`, isBankEntry),
    finderPropose: () => finderRequest(`${base}/finder/scenarios`, isFinderRun, { count: 1 }),
    finderProcess: (id) => finderRequest(`${base}/finder/scenarios/${id}/process`, isRunProgress, {}),
    finderProgress: (runId) => finderRequest(`${base}/finder/runs/${runId}`, isRunProgress),
    finderDiscard: (id) => finderRequest(`${base}/finder/scenarios/${id}/discard`, isBankEntry, {}),
    // A 204 has no body: the response is read as null.
    finderDelete: (id) =>
      finderRequest(`${base}/finder/entries/${id}`, (value): value is null => value === null, undefined, 'DELETE'),
    finderDecide: (id, decision, note) => {
      const body: DecisionRequest = { decision, note: note.trim() || null };
      return finderRequest(`${base}/finder/entries/${id}/decision`, isBankEntry, body);
    },

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
