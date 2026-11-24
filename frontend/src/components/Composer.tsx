import { useLayoutEffect, type FormEvent, type KeyboardEvent, type RefObject } from 'react';
import { ArrowUpIcon, StopIcon } from './icons';

interface Props {
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  onStop: () => void;
  pending: boolean;
  inputRef: RefObject<HTMLTextAreaElement | null>;
  /** The server's limit on a message, once known. */
  maxLength?: number;
}

const MAX_HEIGHT = 200;

export function Composer({ value, onChange, onSubmit, onStop, pending, inputRef, maxLength }: Props) {
  // Grow with the content up to MAX_HEIGHT, then scroll.
  useLayoutEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, MAX_HEIGHT)}px`;
    el.style.overflowY = el.scrollHeight > MAX_HEIGHT ? 'auto' : 'hidden';
  }, [value, inputRef]);

  const length = value.trim().length;
  const tooLong = maxLength !== undefined && length > maxLength;
  const canSend = length > 0 && !tooLong && !pending;
  const nearLimit = maxLength !== undefined && length > maxLength * 0.9;

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (canSend) onSubmit();
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    // Enter sends; Shift+Enter adds a line. Leave Enter alone while an IME is composing.
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      if (canSend) onSubmit();
    }
  }

  return (
    <form className="composer" onSubmit={handleSubmit}>
      <label htmlFor="composer-input" className="sr-only">
        Message
      </label>
      <textarea
        id="composer-input"
        ref={inputRef}
        name="message"
        rows={1}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={handleKeyDown}
        placeholder="Ask about the transaction data…"
        autoComplete="off"
        enterKeyHint="send"
        aria-describedby="composer-hint"
        aria-invalid={tooLong || undefined}
      />
      {nearLimit && (
        <span className="composer-count" data-over={tooLong || undefined} role="status">
          {length.toLocaleString('en-US')} / {maxLength.toLocaleString('en-US')}
          <span className="sr-only">{tooLong ? ' characters: too long to send' : ' characters'}</span>
        </span>
      )}
      {pending ? (
        <button type="button" className="send send-stop" onClick={onStop} aria-label="Stop waiting for the reply">
          <StopIcon />
        </button>
      ) : (
        <button type="submit" className="send" disabled={!canSend} aria-label="Send message">
          <ArrowUpIcon />
        </button>
      )}
    </form>
  );
}
