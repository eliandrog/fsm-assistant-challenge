import { createHttpClient } from './httpClient';
import type { Health, Reply } from './types';

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
}

/** The real API, at the page's own origin. Tests pass a client of their own to <App>. */
export function createClient(): ChatClient {
  return createHttpClient();
}
