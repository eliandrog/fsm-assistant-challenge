import { useCopy } from '../hooks/useCopy';
import { CheckIcon, CopyIcon } from './icons';

interface Props {
  text: string;
  /** What is being copied, for the accessible name: "Copy SQL". */
  label: string;
}

export function CopyButton({ text, label }: Props) {
  const { state, copy } = useCopy();
  const copied = state === 'copied';

  return (
    <button
      type="button"
      className="btn btn-ghost btn-small"
      onClick={() => void copy(text)}
      aria-label={label}
    >
      {copied ? <CheckIcon /> : <CopyIcon />}
      <span aria-hidden="true">
        {copied ? 'Copied' : state === 'failed' ? 'Copy failed' : 'Copy'}
      </span>
      <span className="sr-only" role="status">
        {copied ? 'Copied to clipboard' : state === 'failed' ? 'Could not copy' : ''}
      </span>
    </button>
  );
}
