import type { Cell, ColumnFormat, TableResult } from '../api/types';
import { safeText } from './text';

/**
 * How values are shown. The backend says what a column holds (its format
 * hint); these functions follow the terminal's rendering of the same result
 * (`presentation.format_cell`), so the two interfaces show the same figures.
 * One difference: the terminal prints a fraction as `0.851064 (85.106%)`; a
 * table here shows `85.106%` and keeps the value the query returned in the
 * cell's tooltip (`rawCell`) and in the copied table (`tableAsTsv`).
 */

const LOCALE = 'en-US';
const MAX_CELL_CHARACTERS = 300;

const integer = new Intl.NumberFormat(LOCALE, { maximumFractionDigits: 0 });
const decimal = new Intl.NumberFormat(LOCALE, { maximumFractionDigits: 4 });
const usd = new Intl.NumberFormat(LOCALE, { style: 'currency', currency: 'USD' });

export function formatInteger(value: number): string {
  return integer.format(value);
}

/** Integer cents → "$1,234.56". Negative amounts use a real minus sign. */
export function formatUsdCents(cents: number): string {
  return usd.format(cents / 100).replace('-', '−');
}

/** Fraction 0–1 → "43.24%", with `digits` decimals. null (an undefined ratio) → "—". */
export function formatPercent(fraction: number | null, digits = 2): string {
  if (fraction === null || !Number.isFinite(fraction)) return '—';
  return `${(fraction * 100).toLocaleString(LOCALE, { minimumFractionDigits: digits, maximumFractionDigits: digits })}%`;
}

/** A number at six significant digits, without trailing zeros. */
function significant(value: number): string {
  return String(Number(value.toPrecision(6)));
}

/** Only a value between 0 and 1 is ever shown as a percentage, whatever the hint says. */
function isUnitFraction(value: number): boolean {
  return value >= 0 && value <= 1;
}

function formatPlain(value: number): string {
  if (Number.isInteger(value)) return integer.format(value);
  return value === 0 || Math.abs(value) >= 0.01 ? decimal.format(value) : significant(value);
}

/** "22.5", for a lift. */
export function formatTimes(value: number): string {
  return value.toLocaleString(LOCALE, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
}

export function isNumericCell(value: Cell): boolean {
  return typeof value === 'number';
}

export function formatCell(value: Cell, format: ColumnFormat): string {
  if (value === null) return 'NULL';
  if (typeof value === 'string') return safeText(value, MAX_CELL_CHARACTERS);
  switch (format) {
    case 'usd_cents':
      return formatUsdCents(value);
    case 'fraction':
      // The percentage the terminal prints in brackets, to the same three decimals.
      return isUnitFraction(value) ? formatPercent(value, 3) : formatPlain(value);
    case 'identifier':
      return String(value);
    case 'plain':
      return formatPlain(value);
  }
}

/**
 * The value as the query returned it, for a cell that shows it as a
 * percentage; undefined when the cell already shows the value itself.
 */
export function rawCell(value: Cell, format: ColumnFormat): string | undefined {
  return typeof value === 'number' && format === 'fraction' && isUnitFraction(value) ? String(value) : undefined;
}

/** The tooltip of a figure shown as a percentage. */
export function rawTitle(raw: string): string {
  return `Value returned: ${raw}`;
}

/** Every row as tab-separated text with the values as returned: no percentages, no dollars, no grouping. */
export function tableAsTsv(table: TableResult): string {
  const cell = (value: Cell): string =>
    value === null ? 'NULL' : typeof value === 'string' ? safeText(value) : String(value);
  return [
    table.columns.map((column) => safeText(column.name)).join('\t'),
    ...table.rows.map((row) => table.columns.map((_, i) => cell(row[i] ?? null)).join('\t')),
  ].join('\n');
}

/** Milliseconds → "1.2 s". */
export function formatSeconds(ms: number): string {
  return `${(ms / 1000).toFixed(1)} s`;
}
