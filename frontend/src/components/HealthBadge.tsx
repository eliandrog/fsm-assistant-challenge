import type { HealthState } from '../hooks/useHealth';
import { safeText } from '../lib/text';

interface Pill {
  tone: 'ok' | 'warn' | 'danger' | 'muted';
  label: string;
  title: string;
}

function describe(state: HealthState): Pill[] {
  if (state.state === 'loading') {
    return [{ tone: 'muted', label: 'Checking…', title: 'Asking the server how it is set up.' }];
  }
  if (state.state === 'unreachable') {
    return [
      {
        tone: 'danger',
        label: 'Server unreachable',
        title: 'The assistant’s server did not answer. Start it with `fsm-assistant serve`.',
      },
    ];
  }
  const { health } = state;
  const pills: Pill[] = [];
  if (!health.database.present) {
    pills.push({ tone: 'danger', label: 'Database missing', title: 'The server found no database file.' });
  } else if (!health.database.readable || !health.database.schema_context) {
    pills.push({ tone: 'danger', label: 'Database unreadable', title: 'The server could not read the database.' });
  }
  if (health.mode === 'offline_demo') {
    pills.push({
      tone: 'warn',
      label: 'Offline demo',
      title:
        'No model is connected. A fixed list of questions gets canned model replies, which run through the real SQL guard, database and backtest.',
    });
  } else if (!health.key_configured) {
    pills.push({
      tone: 'danger',
      label: 'No API key',
      title: 'ANTHROPIC_API_KEY is not set on the server, so no question can be answered.',
    });
  } else {
    pills.push({
      tone: 'ok',
      label: safeText(health.model_id, 40),
      title: `Live: each question is sent to ${safeText(health.model_id, 40)} and billed to the server’s API key.`,
    });
  }
  if (health.tracing.enabled) {
    pills.push({
      tone: 'muted',
      label: 'Tracing on',
      title: `Each turn’s question, SQL, codes and timings are sent to LangSmith ${safeText(health.tracing.destination ?? '', 80)}. Result rows never are.`,
    });
  }
  return pills;
}

/** How the server is set up: model or offline demo, key, database, tracing. */
export function HealthBadge({ health }: { health: HealthState }) {
  return (
    <ul className="health" aria-label="Server status">
      {describe(health).map((pill) => (
        <li key={pill.label} className={`badge badge-${pill.tone}`} title={pill.title}>
          <span className="health-dot" aria-hidden="true" />
          {pill.label}
          <span className="sr-only">. {pill.title}</span>
        </li>
      ))}
    </ul>
  );
}
