import { safeText } from '../lib/text';
import { BrandMark } from './icons';

export const EXAMPLES: { title: string; prompt: string }[] = [
  {
    title: 'Size the problem',
    prompt: 'How many transactions are labelled as fraud?',
  },
  {
    title: 'Find where fraud concentrates',
    prompt: 'Where is fraud concentrated?',
  },
  {
    title: 'Check a trend',
    prompt: 'How does fraud change month by month in 2019?',
  },
  {
    title: 'Turn a pattern into a rule',
    prompt: 'Propose a rule for high-value online transactions and backtest it',
  },
];

interface Props {
  onPick: (prompt: string) => void;
  /** The questions the offline demo can answer; empty when a model is connected. */
  demoQuestions: string[];
}

export function EmptyState({ onPick, demoQuestions }: Props) {
  const isDemo = demoQuestions.length > 0;
  return (
    <section className="empty" aria-labelledby="empty-title">
      <BrandMark size={40} />
      <h2 id="empty-title">Ask the transaction data a&nbsp;question</h2>
      <p className="empty-lede">
        Kestrel turns plain English into SQL over the 2019 card transactions, shows the query
        next to its result, finds where labelled fraud is concentrated, and backtests a fraud
        rule against the labelled history.
      </p>
      {isDemo && (
        <p className="empty-note">
          Offline demo: no model is connected. These questions get canned model replies, which
          run through the real SQL guard, database and backtest. Any other question gets this
          list back.
        </p>
      )}
      <ul className={isDemo ? 'examples examples-demo' : 'examples'} aria-label="Example questions">
        {isDemo
          ? demoQuestions.map((question) => (
              <li key={question}>
                <button type="button" className="example" onClick={() => onPick(question)}>
                  <span className="example-prompt">{safeText(question, 200)}</span>
                </button>
              </li>
            ))
          : EXAMPLES.map((example) => (
              <li key={example.prompt}>
                <button type="button" className="example" onClick={() => onPick(example.prompt)}>
                  <span className="example-title">{example.title}</span>
                  <span className="example-prompt">{example.prompt}</span>
                </button>
              </li>
            ))}
      </ul>
    </section>
  );
}
