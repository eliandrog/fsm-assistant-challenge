import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ChatClient } from '../api/client';
import type { Reply } from '../api/types';
import {
  closeOpenTurn,
  loadConversations,
  newId,
  saveConversations,
  titleFrom,
  type ChatMessage,
  type Conversation,
} from '../lib/conversations';

interface State {
  /** Only conversations with at least one message. */
  conversations: Conversation[];
  /** The conversation on screen. A fresh id that is not in `conversations` is an unsent new chat. */
  activeId: string;
}

const UNDO_MS = 8000;
const NO_MESSAGES: ChatMessage[] = [];

function unexpectedFailure(): Reply {
  return {
    kind: 'failure',
    failure: {
      reason: 'unreadable',
      message: 'The interface hit an unexpected problem. Retry the question.',
    },
  };
}

function isAbort(err: unknown): boolean {
  return err instanceof DOMException && err.name === 'AbortError';
}

function withId(set: ReadonlySet<string>, id: string): ReadonlySet<string> {
  return set.has(id) ? set : new Set(set).add(id);
}

function withoutId(set: ReadonlySet<string>, id: string): ReadonlySet<string> {
  if (!set.has(id)) return set;
  const next = new Set(set);
  next.delete(id);
  return next;
}

function initialState(): State {
  const stored = loadConversations();
  return { conversations: stored.conversations, activeId: stored.activeId ?? newId() };
}

/**
 * All conversations, the active one, and the requests in flight.
 *
 * Requests are tracked per conversation id, so a reply always lands in the
 * conversation that asked, whichever one is on screen when it arrives.
 * Conversations are saved to localStorage; pending, unread and undo state is
 * transient and never stored. `onFailure` is called when a question got no
 * turn result back (the server could not be reached, or refused it).
 */
export function useConversations(client: ChatClient, onFailure?: () => void) {
  const [state, setState] = useState<State>(initialState);
  const [pendingIds, setPendingIds] = useState<ReadonlySet<string>>(() => new Set());
  const [unreadIds, setUnreadIds] = useState<ReadonlySet<string>>(() => new Set());
  /** The reply that just arrived on screen; only this one animates in. */
  const [freshId, setFreshId] = useState<string | null>(null);
  const [removed, setRemoved] = useState<Conversation | null>(null);

  const controllers = useRef(new Map<string, AbortController>());
  // Bumped per request and on delete, so a late reply from an old run is dropped.
  const runs = useRef(new Map<string, number>());
  const activeIdRef = useRef(state.activeId);

  useEffect(() => {
    activeIdRef.current = state.activeId;
  }, [state.activeId]);

  useEffect(() => {
    saveConversations(state);
  }, [state]);

  useEffect(() => {
    if (!removed) return;
    const timer = setTimeout(() => setRemoved(null), UNDO_MS);
    return () => clearTimeout(timer);
  }, [removed]);

  const ask = useCallback(
    async (conversationId: string, question: string) => {
      const thisRun = (runs.current.get(conversationId) ?? 0) + 1;
      runs.current.set(conversationId, thisRun);
      const abort = new AbortController();
      controllers.current.set(conversationId, abort);
      setPendingIds((prev) => withId(prev, conversationId));

      let reply: Reply | null;
      try {
        reply = await client.sendMessage(conversationId, question, { signal: abort.signal });
      } catch (err) {
        reply = isAbort(err) ? null : unexpectedFailure();
      }

      if (runs.current.get(conversationId) !== thisRun) return;
      controllers.current.delete(conversationId);

      const message: ChatMessage = {
        id: newId(),
        role: 'assistant',
        question,
        reply,
      };
      const updatedAt = Date.now();
      setState((prev) => ({
        ...prev,
        conversations: prev.conversations.map((c) =>
          c.id === conversationId ? { ...c, messages: [...c.messages, message], updatedAt } : c,
        ),
      }));
      setPendingIds((prev) => withoutId(prev, conversationId));

      if (activeIdRef.current === conversationId) setFreshId(message.id);
      else setUnreadIds((prev) => withId(prev, conversationId));
      if (reply?.kind === 'failure') onFailure?.();
    },
    [client, onFailure],
  );

  const { activeId } = state;
  const pending = pendingIds.has(activeId);

  const send = useCallback(
    (text: string) => {
      const question = text.trim();
      if (!question || pending) return;
      const message: ChatMessage = { id: newId(), role: 'user', text: question };
      const updatedAt = Date.now();
      setState((prev) => {
        const exists = prev.conversations.some((c) => c.id === activeId);
        return {
          ...prev,
          conversations: exists
            ? prev.conversations.map((c) =>
                c.id === activeId ? { ...c, messages: [...c.messages, message], updatedAt } : c,
              )
            : [
                { id: activeId, title: titleFrom(question), messages: [message], updatedAt },
                ...prev.conversations,
              ],
        };
      });
      void ask(activeId, question);
    },
    [ask, activeId, pending],
  );

  /** Replace a failed or unanswered assistant message with a fresh attempt. */
  const retry = useCallback(
    (messageId: string) => {
      if (pending) return;
      const conversation = state.conversations.find((c) => c.id === activeId);
      const target = conversation?.messages.find((m) => m.id === messageId);
      if (!target || target.role !== 'assistant') return;
      setState((prev) => ({
        ...prev,
        conversations: prev.conversations.map((c) =>
          c.id === activeId ? { ...c, messages: c.messages.filter((m) => m.id !== messageId) } : c,
        ),
      }));
      void ask(activeId, target.question);
    },
    [ask, activeId, pending, state.conversations],
  );

  /** Stops the request of the conversation on screen; others keep running. */
  const stop = useCallback(() => controllers.current.get(activeId)?.abort(), [activeId]);

  const select = useCallback((id: string) => {
    setState((prev) => (prev.activeId === id ? prev : { ...prev, activeId: id }));
    setUnreadIds((prev) => withoutId(prev, id));
    setFreshId(null);
  }, []);

  /** Starts an unsent conversation. It joins the list when its first message is sent. */
  const newChat = useCallback(() => {
    setState((prev) => ({ ...prev, activeId: newId() }));
    setFreshId(null);
  }, []);

  const remove = useCallback(
    (id: string) => {
      const conversation = state.conversations.find((c) => c.id === id);
      if (!conversation) return;
      // Cancel its request and make sure a late reply cannot resurrect it.
      runs.current.set(id, (runs.current.get(id) ?? 0) + 1);
      controllers.current.get(id)?.abort();
      controllers.current.delete(id);
      setPendingIds((prev) => withoutId(prev, id));
      setUnreadIds((prev) => withoutId(prev, id));
      setRemoved(conversation);
      setState((prev) => ({
        conversations: prev.conversations.filter((c) => c.id !== id),
        activeId: prev.activeId === id ? newId() : prev.activeId,
      }));
    },
    [state.conversations],
  );

  const undoRemove = useCallback(() => {
    if (!removed) return;
    const restored = closeOpenTurn(removed);
    setState((prev) => ({
      conversations: [restored, ...prev.conversations.filter((c) => c.id !== restored.id)],
      activeId: restored.id,
    }));
    setFreshId(null);
    setRemoved(null);
  }, [removed]);

  const conversations = useMemo(
    () => [...state.conversations].sort((a, b) => b.updatedAt - a.updatedAt),
    [state.conversations],
  );
  const messages = state.conversations.find((c) => c.id === activeId)?.messages ?? NO_MESSAGES;

  return {
    conversations,
    activeId,
    messages,
    pending,
    pendingIds,
    unreadIds,
    freshId,
    removed,
    send,
    retry,
    stop,
    select,
    newChat,
    remove,
    undoRemove,
  };
}
