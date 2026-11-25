import { useId, useState } from 'react';
import { DELETE_WARNING } from '../../lib/finder';

interface Props {
  /** What is deleted, for the buttons' accessible names. */
  subject: string;
  disabled?: boolean;
  onDelete: () => void;
}

/** Delete, in two steps on the page itself: the first click asks, the second removes. */
export function DeleteButton({ subject, disabled, onDelete }: Props) {
  const [asking, setAsking] = useState(false);
  const warningId = useId();
  if (!asking) {
    return (
      <button
        type="button"
        className="btn btn-ghost btn-danger"
        disabled={disabled}
        aria-label={`Delete ${subject}`}
        onClick={() => setAsking(true)}
      >
        Delete…
      </button>
    );
  }
  return (
    <div className="confirm" role="group" aria-label={`Confirm deleting ${subject}`}>
      <p id={warningId}>{DELETE_WARNING}</p>
      <div className="review-actions">
        <button type="button" className="btn btn-danger-solid" aria-describedby={warningId} onClick={onDelete}>
          Delete for good
        </button>
        <button type="button" className="btn btn-outline" autoFocus onClick={() => setAsking(false)}>
          Cancel
        </button>
      </div>
    </div>
  );
}
