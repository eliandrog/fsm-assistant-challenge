import { useMemo } from 'react';
import { tokenizeSql } from '../lib/highlightSql';

/** Syntax-coloured SQL. Scrolls horizontally instead of wrapping. */
export function SqlCode({ sql, label }: { sql: string; label: string }) {
  const tokens = useMemo(() => tokenizeSql(sql), [sql]);
  return (
    // tabIndex: lets keyboard users scroll wide statements.
    <pre className="sql-code" tabIndex={0} role="region" aria-label={label} translate="no">
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
