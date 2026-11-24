import { BrandMark } from './icons';

export function Thinking() {
  return (
    <div className="msg msg-assistant thinking">
      <div className="msg-meta">
        <BrandMark size={20} />
        <span className="thinking-label">Working on it</span>
        <span className="thinking-dots" aria-hidden="true">
          <i />
          <i />
          <i />
        </span>
      </div>
    </div>
  );
}
