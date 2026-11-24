import { useEffect, type ReactNode } from 'react';
import type { ClientFailure, ErrorKind, FailureReason, Reply, TurnResult } from '../api/types';
import { useReveal } from '../hooks/useReveal';
import { DEMO_MODEL_ID } from '../lib/evidence';
import { formatInteger, formatSeconds } from '../lib/format';
import { safeBlock, safeText } from '../lib/text';
import { AlertIcon, BrandMark, RetryIcon, ShieldIcon } from './icons';
import { ResultsTable } from './ResultsTable';
import { RuleCard } from './RuleCard';
import { SqlBlock } from './SqlBlock';

interface Props {
  /** null: no reply arrived (the user stopped the request, or the page was reloaded). */
  reply: Reply | null;
  /** True for a reply that has just arrived: it reveals progressively. */
  animate: boolean;
  /** Only the newest message offers Retry and quick replies. */
  isLatest: boolean;
  busy: boolean;
  /** The server answered this as a first turn although the conversation has earlier ones. */
  memoryLost: boolean;
  onRetry: () => void;
  onSuggestion: (text: string) => void;
  /** Called once the reply is fully on screen. */
  onSettled?: () => void;
}

const ERROR_TITLES: Record<ErrorKind, string> = {
  model_unavailable: 'The model didn’t respond',
  model_output: 'The model’s reply could not be used',
  timeout: 'The query took too long',
  sql_execution: 'The query failed to run',
  sql_rejected: 'The query was blocked',
  invalid_input: 'The message was not accepted',
  internal: 'Something went wrong',
};

const FAILURE_TITLES: Record<FailureReason, string> = {
  network: 'Can’t reach the server',
  busy: 'Still answering the previous question',
  invalid: 'The message was not accepted',
  server: 'The server failed',
  unreadable: 'The reply could not be read',
};

/** Asking again cannot help when the message itself was refused. */
const NO_RETRY: ReadonlySet<ErrorKind> = new Set<ErrorKind>(['invalid_input']);

function RetryButton({ onRetry, busy }: { onRetry: () => void; busy: boolean }) {
  return (
    <button type="button" className="btn btn-outline" onClick={onRetry} disabled={busy}>
      <RetryIcon /> Retry
    </button>
  );
}

function Problem({
  tone,
  title,
  children,
}: {
  tone: 'error' | 'blocked' | 'neutral';
  title: string;
  children: ReactNode;
}) {
  const Icon = tone === 'blocked' ? ShieldIcon : AlertIcon;
  return (
    <div className={`callout callout-${tone}`}>
      <Icon className="callout-icon" />
      <div>
        <p className="callout-title">{title}</p>
        {children}
      </div>
    </div>
  );
}

/** Tool, refusals, model, requests, tokens and time: what produced this answer. */
function Footer({ result }: { result: TurnResult }) {
  const { meta } = result;
  const refused = result.refused.length > 0 ? `refused: ${result.refused.join(', ')}` : null;
  const writes = result.write_attempts;
  const stopped =
    writes > 0 ? `${writes} write ${writes === 1 ? 'attempt' : 'attempts'} stopped by the guard` : null;
  const parts = [
    `tool: ${result.tool ?? 'none'}`,
    refused,
    stopped,
    safeText(meta.model_id, 60),
    `${meta.model_calls} model ${meta.model_calls === 1 ? 'request' : 'requests'}`,
    `${formatInteger(meta.input_tokens)} in, ${formatInteger(meta.output_tokens)} out tokens`,
    formatSeconds(meta.elapsed_ms),
  ].filter((part): part is string => part !== null);
  return (
    <p className="turn-meta" aria-label="How this answer was produced">
      {parts.map((part, i) => (
        <span key={i}>{part}</span>
      ))}
    </p>
  );
}

function Suggestions({
  result,
  disabled,
  onSuggestion,
}: {
  result: TurnResult;
  disabled: boolean;
  onSuggestion: (text: string) => void;
}) {
  if (result.suggestions.length === 0) return null;
  return (
    <div className="suggest">
      <p className="suggest-label">
        {result.status === 'needs_clarification' ? 'Pick one, or answer in your own words' : 'Ask next'}
      </p>
      <ul className="suggestions" aria-label="Suggested questions">
        {result.suggestions.map((suggestion, i) => (
          <li key={i}>
            {/* The chip sends the exact text it was given, and shows it with nothing hidden. */}
            <button type="button" className="chip" disabled={disabled} onClick={() => onSuggestion(suggestion)}>
              {safeText(suggestion, 300)}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** What the model wrote, apart from what code computed. */
function ModelText({ result }: { result: TurnResult }) {
  if (!result.explanation && result.assumptions.length === 0) return null;
  return (
    <section className="model-text" aria-label="Model text, not checked">
      <p className="model-text-label">
        <span className="badge badge-muted">Model text</span> Written by the model and not checked
        against the data.
      </p>
      {result.explanation && (
        <p>
          <strong>Explanation.</strong> {safeBlock(result.explanation)}
        </p>
      )}
      {result.assumptions.length > 0 && (
        <>
          <p>
            <strong>Assumptions.</strong>
          </p>
          <ul>
            {result.assumptions.map((assumption, i) => (
              <li key={i}>{safeBlock(assumption)}</li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

type TurnProps = Omit<Props, 'reply' | 'memoryLost'> & { result: TurnResult };

function Turn({ result, animate, isLatest, busy, onRetry, onSuggestion, onSettled }: TurnProps) {
  const { status, error } = result;
  // An error or a refusal is shown in a callout, whole; an answer line reveals.
  const inCallout = status === 'error' || status === 'rejected' || status === 'cannot_answer';
  const answer = safeBlock(result.answer);
  const { shown, done } = useReveal(answer, animate && !inCallout);

  useEffect(() => {
    if (done) onSettled?.();
    // Only the moment the reveal finishes matters, not later callback identities.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [done]);

  const isScan = result.tool === 'profile_fraud';

  return (
    <>
      {status === 'ok' && answer && <p className="answer">{shown}</p>}
      {status === 'needs_clarification' && (
        <p className="answer">
          <span className="answer-kicker">The assistant asks</span>
          {shown}
        </p>
      )}

      {done && (
        <div className="answer-parts">
          {status === 'error' && (
            <Problem tone="error" title={ERROR_TITLES[error?.kind ?? 'internal'] ?? ERROR_TITLES.internal}>
              <p>{safeBlock(error?.message ?? result.answer)}</p>
              {error?.detail && (
                <p className="callout-detail">
                  SQLite said: <code>{safeText(error.detail, 500)}</code>
                </p>
              )}
              {isLatest && !NO_RETRY.has(error?.kind ?? 'internal') && (
                <RetryButton onRetry={onRetry} busy={busy} />
              )}
            </Problem>
          )}
          {status === 'rejected' && (
            <Problem tone="blocked" title="Blocked by the SQL safety guard">
              <p>{safeBlock(error?.message ?? result.answer)}</p>
              <p className="callout-detail">Nothing was run against the database.</p>
            </Problem>
          )}
          {status === 'cannot_answer' && (
            <Problem tone="neutral" title="The assistant can’t answer this">
              <p>{answer}</p>
              {result.answer_source === 'model' && (
                <p className="callout-detail">This reason was written by the model.</p>
              )}
            </Problem>
          )}

          {result.sql && (
            <SqlBlock
              sql={result.sql}
              title={isScan ? 'SQL that ran (the scan’s profile statement)' : 'SQL that ran'}
            />
          )}
          {result.table && (
            <ResultsTable table={result.table} title={isScan ? 'Segments where fraud is concentrated' : 'Results'} />
          )}
          {result.rule && result.where_clause && (
            <RuleCard rule={result.rule} whereClause={result.where_clause} />
          )}

          {result.notes.length > 0 && (
            <section className="notes" aria-label="Notes">
              <h3 className="panel-title">Notes</h3>
              <ul>
                {result.notes.map((note, i) => (
                  <li key={i}>{safeBlock(note)}</li>
                ))}
              </ul>
            </section>
          )}

          {result.caveats.length > 0 && (
            <ul className="caveats" aria-label="Caveats">
              {result.caveats.map((caveat) => (
                <li key={caveat.code} className="caveat" data-code={caveat.code}>
                  <AlertIcon className="callout-icon" />
                  <p>
                    <span className="caveat-code" translate="no">
                      {caveat.code}
                    </span>
                    {safeBlock(caveat.message)}
                  </p>
                </li>
              ))}
            </ul>
          )}

          <ModelText result={result} />
          <Suggestions result={result} disabled={busy || !isLatest} onSuggestion={onSuggestion} />
          <Footer result={result} />
        </div>
      )}
    </>
  );
}

function Failure({ failure, canRetry, busy, onRetry }: { failure: ClientFailure; canRetry: boolean; busy: boolean; onRetry: () => void }) {
  return (
    <Problem tone="error" title={FAILURE_TITLES[failure.reason] ?? FAILURE_TITLES.server}>
      <p>{safeBlock(failure.message)}</p>
      {canRetry && failure.reason !== 'invalid' && <RetryButton onRetry={onRetry} busy={busy} />}
    </Problem>
  );
}

export function AssistantMessage({
  reply,
  animate,
  isLatest,
  busy,
  memoryLost,
  onRetry,
  onSuggestion,
  onSettled,
}: Props) {
  const isDemo = reply?.kind === 'turn' && reply.result.meta.model_id === DEMO_MODEL_ID;
  return (
    <article className="msg msg-assistant" aria-label="Assistant reply">
      <header className="msg-meta">
        <BrandMark size={20} />
        <span className="msg-author" translate="no">
          Kestrel
        </span>
        {isDemo && (
          <span
            className="badge badge-warn"
            title="A canned model reply from the offline demo. The SQL guard, the database and the figures are real."
          >
            Offline demo
          </span>
        )}
      </header>

      <div className="msg-body">
        {memoryLost && (
          <p className="memory-note">
            The server was restarted since the earlier questions, so this one was answered on its
            own, without them as context.
          </p>
        )}
        {reply === null ? (
          <div className="stopped">
            <p>No reply arrived for this question.</p>
            {isLatest && <RetryButton onRetry={onRetry} busy={busy} />}
          </div>
        ) : reply.kind === 'failure' ? (
          <Failure failure={reply.failure} canRetry={isLatest} busy={busy} onRetry={onRetry} />
        ) : (
          <Turn
            result={reply.result}
            animate={animate}
            isLatest={isLatest}
            busy={busy}
            onRetry={onRetry}
            onSuggestion={onSuggestion}
            onSettled={onSettled}
          />
        )}
      </div>
    </article>
  );
}
