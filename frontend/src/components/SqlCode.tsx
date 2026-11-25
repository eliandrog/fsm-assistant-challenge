import { useMemo } from 'react';
import { tokenizeSql } from '../lib/highlightSql';

/** Syntax-coloured SQL. Scrolls horizontally inside its own box, or wraps when `wrap` is set. */
export function SqlCode({ sql, label, wrap = false }: { sql: string; label: string; wrap?: boolean }) {
  const tokens = useMemo(() => tokenizeSql(sql), [sql]);
  return (
    // tabIndex: lets keyboard users scroll wide statements.
    <pre className={wrap ? 'sql-code sql-wrap' : 'sql-code'} tabIndex={0} role="region" aria-label={label} translate="no">
      <code>
        {tokens.map((token, i) =>
          token.type === 'plain' ? (
            token.text
          ) : (
            <span key={i} className={`tok-${token.type}`}>
              {token.text}
            </span>
          ),
        )}
      </code>
    </pre>
  );
}
