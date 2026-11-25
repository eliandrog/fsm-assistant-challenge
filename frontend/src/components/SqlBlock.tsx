import { useId, useState } from 'react';
import { safeBlock } from '../lib/text';
import { CopyButton } from './CopyButton';
import { ChevronIcon } from './icons';
import { SqlCode } from './SqlCode';

interface Props {
  sql: string;
  /** Heading for the block. */
  title?: string;
  /** Short remark shown next to the title. */
  note?: string;
  /** Whether it starts open. By default a short statement does and a long one does not. */
  defaultOpen?: boolean;
}

/** A statement longer than this starts collapsed: the scan's profile is about 70 lines. */
const COLLAPSE_ABOVE_LINES = 12;

export function SqlBlock({ sql, title = 'SQL that ran', note, defaultOpen }: Props) {
  const text = safeBlock(sql);
  const lines = text.split('\n').length;
  const [open, setOpen] = useState(defaultOpen ?? lines <= COLLAPSE_ABOVE_LINES);
  const bodyId = useId();

  return (
    <section className="panel sql-block">
      <header className="panel-bar">
        <button
          type="button"
          className="disclosure"
          aria-expanded={open}
          aria-controls={bodyId}
          onClick={() => setOpen((v) => !v)}
        >
          <ChevronIcon className="disclosure-chevron" />
          <span className="panel-title">{title}</span>
          <span className="panel-note">{note ?? `${lines} ${lines === 1 ? 'line' : 'lines'}`}</span>
        </button>
        <CopyButton text={sql} label={`Copy ${title}`} />
      </header>
      <div id={bodyId} hidden={!open}>
        <SqlCode sql={text} label={title} />
      </div>
    </section>
  );
}
