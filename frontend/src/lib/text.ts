/**
 * Text that code in this interface did not write (a model's words, a value
 * from the database) is always rendered as a text node, never as HTML. This
 * makes what a text node would hide visible as well: control characters, and
 * the invisible and direction-changing characters that can make text read as
 * something else. The backend does the same for the terminal; the API sends
 * the strings raw.
 */

// C0 and C1 controls, zero-width and bidirectional formatting characters, the byte-order mark.
// eslint-disable-next-line no-control-regex
const HIDDEN = /[\u0000-\u001f\u007f-\u009f\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]/g;

function escape(character: string): string {
  if (character === '\n') return '\\n';
  if (character === '\t') return '\\t';
  if (character === '\r') return '\\r';
  const code = character.charCodeAt(0);
  return code < 0x100
    ? `\\x${code.toString(16).padStart(2, '0')}`
    : `\\u${code.toString(16).padStart(4, '0')}`;
}

/** `text` on one line with every hidden character written out, cut to `limit` characters. */
export function safeText(text: string, limit?: number): string {
  const cleaned = text.replace(HIDDEN, escape);
  return limit !== undefined && cleaned.length > limit ? `${cleaned.slice(0, limit - 1)}…` : cleaned;
}

/** As `safeText`, for prose and SQL: line breaks and tabs are kept as they are. */
export function safeBlock(text: string): string {
  return text.replace(/\r\n?/g, '\n').replace(HIDDEN, (c) => (c === '\n' || c === '\t' ? c : escape(c)));
}
