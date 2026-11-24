import { isRecord, isTurnResult } from '../api/guards';
import type { Reply } from '../api/types';
import { readJson, writeJson } from './storage';

export type ChatMessage =
  | { id: string; role: 'user'; text: string }
  | {
      id: string;
      role: 'assistant';
      /** The user text this replies to; used by Retry. */
      question: string;
      /** null when no reply arrived (stopped, or interrupted by a reload). */
      reply: Reply | null;
    };

export interface Conversation {
  /** Also the `conversation_id` sent to the backend. */
  id: string;
  /** From the first user message. */
  title: string;
  messages: ChatMessage[];
  /** Epoch ms of the last message; orders the sidebar. */
  updatedAt: number;
}

export interface StoredState {
  conversations: Conversation[];
  activeId: string | null;
}

/** Bump the suffix when the stored shape changes; old data is then ignored. */
export const STORAGE_KEY = 'kestrel:conversations:v2';
const MAX_STORED = 50;
const TITLE_LENGTH = 60;

let counter = 0;
export function newId(): string {
  counter += 1;
  return typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `id-${Date.now()}-${counter}`;
}

export function titleFrom(text: string): string {
  const flat = text.replace(/\s+/g, ' ').trim();
  return flat.length > TITLE_LENGTH ? `${flat.slice(0, TITLE_LENGTH - 1).trimEnd()}…` : flat;
}

/** A question with no reply (reload or delete mid-request) gets a "no reply" row, which offers Retry. */
export function closeOpenTurn(conversation: Conversation): Conversation {
  const last = conversation.messages[conversation.messages.length - 1];
  if (!last || last.role !== 'user') return conversation;
  return {
    ...conversation,
    messages: [
      ...conversation.messages,
      { id: newId(), role: 'assistant', question: last.text, reply: null },
    ],
  };
}

/* ---------- Reading stored data: trust nothing ---------- */

function isReply(value: unknown): value is Reply {
  if (!isRecord(value)) return false;
  if (value.kind === 'turn') return isTurnResult(value.result);
  return (
    value.kind === 'failure' &&
    isRecord(value.failure) &&
    typeof value.failure.reason === 'string' &&
    typeof value.failure.message === 'string'
  );
}

function isMessage(value: unknown): value is ChatMessage {
  if (!isRecord(value) || typeof value.id !== 'string') return false;
  if (value.role === 'user') return typeof value.text === 'string';
  return (
    value.role === 'assistant' &&
    typeof value.question === 'string' &&
    (value.reply === null || isReply(value.reply))
  );
}

function isConversation(value: unknown): value is Conversation {
  return (
    isRecord(value) &&
    typeof value.id === 'string' &&
    typeof value.title === 'string' &&
    typeof value.updatedAt === 'number' &&
    Array.isArray(value.messages) &&
    value.messages.length > 0 &&
    value.messages.every(isMessage)
  );
}

/** Returns what can be salvaged; a malformed conversation is dropped, not fatal. */
export function loadConversations(): StoredState {
  const stored = readJson(STORAGE_KEY);
  if (!isRecord(stored) || !Array.isArray(stored.conversations)) {
    return { conversations: [], activeId: null };
  }
  const conversations = stored.conversations.filter(isConversation).map(closeOpenTurn);
  const activeId =
    typeof stored.activeId === 'string' && conversations.some((c) => c.id === stored.activeId)
      ? stored.activeId
      : null;
  return { conversations, activeId };
}

export function saveConversations(state: StoredState): void {
  const conversations = [...state.conversations]
    .sort((a, b) => b.updatedAt - a.updatedAt)
    .slice(0, MAX_STORED);
  writeJson(STORAGE_KEY, { conversations, activeId: state.activeId });
}
