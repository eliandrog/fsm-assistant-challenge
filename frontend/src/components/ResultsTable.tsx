import { useState } from 'react';
import type { TableResult } from '../api/types';
import { formatCell, formatInteger, isNumericCell, rawCell, rawTitle, tableAsTsv } from '../lib/format';
import { safeText } from '../lib/text';
import { CopyButton } from './CopyButton';
import { TableScroll } from './TableScroll';

/** Rows rendered before the analyst asks for the rest. Keeps long results light. */
const INITIAL_ROWS = 25;
/** A text cell longer than this may wrap; shorter ones stay on one line. */
const WRAP_ABOVE_CHARACTERS = 14;
/** A text cell longer than this carries its text as a tooltip: it may be cut after four lines. */
const TITLE_ABOVE_CHARACTERS = 60;

interface Props {
  table: TableResult;
  title?: string;
}

export function ResultsTable({ table, title = 'Results' }: Props) {
  const [expanded, setExpanded] = useState(false);
  const { columns, rows, row_count: rowCount, truncated } = table;

  // A column is right-aligned when it holds numbers and nothing else.
  const numeric = columns.map(
    (_, i) => rows.some((row) => isNumericCell(row[i] ?? null)) && rows.every((row) => typeof row[i] !== 'string'),
  );
  const visible = expanded ? rows : rows.slice(0, INITIAL_ROWS);
  const hidden = rows.length - visible.length;
  const hasMoney = columns.some((column) => column.format === 'usd_cents');
  const hasFractions = columns.some((column, i) => rows.some((row) => rawCell(row[i] ?? null, column.format)));
  const countLabel = `${formatInteger(rowCount)} ${rowCount === 1 ? 'row' : 'rows'}`;

  return (
    <section className="panel results">
      <header className="panel-bar">
        <h3 className="panel-title">{title}</h3>
        <span className="panel-actions">
          <span className="panel-meta">{truncated ? `First ${countLabel}` : countLabel}</span>
          {rows.length > 0 && <CopyButton text={tableAsTsv(table)} label="Copy table with the values as returned" />}
        </span>
      </header>

      {truncated && (
        <p className="notice notice-warn">
          Capped at the first {countLabel}: more rows match than are shown.
        </p>
      )}

      {rows.length === 0 ? (
        <p className="results-empty">The query ran and returned no rows.</p>
      ) : (
        <TableScroll label={`${title}, ${countLabel}`}>
          <table>
            <thead>
              <tr>
                {columns.map((column, i) => (
                  <th key={i} scope="col" className={numeric[i] ? 'num' : undefined} translate="no">
                    {safeText(column.name, 80)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {visible.map((row, r) => (
                <tr key={r}>
                  {columns.map((column, c) => {
                    const cell = row[c] ?? null;
                    const className =
                      [numeric[c] && 'num', cell === null && 'null'].filter(Boolean).join(' ') || undefined;
                    const text = formatCell(cell, column.format);
                    if (typeof cell === 'string') {
                      return (
                        <td key={c} className={className}>
                          <span
                            className={text.length > WRAP_ABOVE_CHARACTERS ? 'cell-text cell-text-long' : 'cell-text'}
                            title={text.length > TITLE_ABOVE_CHARACTERS ? text : undefined}
                          >
                            {text}
                          </span>
                        </td>
                      );
                    }
                    const raw = rawCell(cell, column.format);
                    return (
                      <td key={c} className={className} title={raw && rawTitle(raw)}>
                        {text}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </TableScroll>
      )}

      {(hidden > 0 || hasMoney || hasFractions) && (
        <footer className="panel-foot">
          {hasMoney && <span>Amounts are stored in cents and shown in US dollars.</span>}
          {hasFractions && (
            <span>
              Fractions between 0 and 1 are shown as percentages. The value the query returned is in each
              cell’s tooltip and in the copied table.
            </span>
          )}
          {hidden > 0 && (
            <button type="button" className="btn btn-ghost btn-small" onClick={() => setExpanded(true)}>
              Show All {formatInteger(rows.length)} Rows
            </button>
          )}
        </footer>
      )}
    </section>
  );
}
