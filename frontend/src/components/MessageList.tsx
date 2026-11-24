import { useCallback, useEffect, useRef } from 'react';
import type { ChatMessage } from '../lib/conversations';
import { safeBlock } from '../lib/text';
import { AssistantMessage } from './AssistantMessage';
import { Thinking } from './Thinking';

interface Props {
  messages: ChatMessage[];
  pending: boolean;
  /** Id of the reply that just arrived, if any. */
  freshId: string | null;
  onRetry: (messageId: string) => void;
  onSuggestion: (text: string) => void;
}

/**
 * The server keeps a conversation's memory in its process. When it answers
 * message `index` as its first turn although an earlier question of this
 * conversation was answered, it was restarted in between.
 */
function memoryLost(messages: ChatMessage[], index: number): boolean {
  const message = messages[index];
  if (message?.role !== 'assistant' || message.reply?.kind !== 'turn') return false;
  if (message.reply.result.meta.turn !== 1) return false;
  return messages
    .slice(0, index)
    .some((earlier) => earlier.role === 'assistant' && earlier.reply?.kind === 'turn');
}

function scrollBehavior(): ScrollBehavior {
  return typeof window.matchMedia === 'function' &&
    !window.matchMedia('(prefers-reduced-motion: reduce)').matches
    ? 'smooth'
    : 'auto';
}

export function MessageList({ messages, pending, freshId, onRetry, onSuggestion }: Props) {
  const questionRef = useRef<HTMLLIElement>(null);
  const last = messages[messages.length - 1];
  const lastQuestion = messages.findLastIndex((m) => m.role === 'user');

  // Keep the newest turn in view, anchored on its question so the reply can be
  // read from the top. Runs when a message is added and again once the reply
  // has finished revealing (its SQL and table add height after the text).
  // The first scroll after opening a conversation is instant; later ones glide.
  const opened = useRef(false);
  const scrollToTurn = useCallback(() => {
    questionRef.current?.scrollIntoView?.({
      block: 'start',
      behavior: opened.current ? scrollBehavior() : 'auto',
    });
    opened.current = true;
  }, []);

  useEffect(scrollToTurn, [last?.id, scrollToTurn]);

  return (
    <>
      <h2 className="sr-only">Conversation</h2>
      <ol className="thread" aria-label="Conversation">
        {messages.map((message, i) => {
          const isLatest = i === messages.length - 1;
          return (
            <li key={message.id} ref={i === lastQuestion ? questionRef : undefined} className="thread-item">
              {message.role === 'user' ? (
                <div className="msg msg-user">
                  <span className="sr-only">You asked: </span>
                  <p>{safeBlock(message.text)}</p>
                </div>
              ) : (
                <AssistantMessage
                  reply={message.reply}
                  animate={message.id === freshId}
                  isLatest={isLatest && !pending}
                  busy={pending}
                  memoryLost={memoryLost(messages, i)}
                  onRetry={() => onRetry(message.id)}
                  onSuggestion={onSuggestion}
                  onSettled={isLatest ? scrollToTurn : undefined}
                />
              )}
            </li>
          );
        })}
        {pending && (
          <li className="thread-item">
            <Thinking />
          </li>
        )}
      </ol>
    </>
  );
}
