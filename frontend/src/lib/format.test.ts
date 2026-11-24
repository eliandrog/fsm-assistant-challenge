import type { TableResult } from '../api/types';
import {
  formatCell,
  formatPercent,
  formatSeconds,
  formatTimes,
  formatUsdCents,
  rawCell,
  rawTitle,
  tableAsTsv,
} from './format';
import { safeBlock, safeText } from './text';

describe('format', () => {
  it('shows integer cents as dollars', () => {
    expect(formatUsdCents(100000)).toBe('$1,000.00');
    expect(formatUsdCents(5)).toBe('$0.05');
    expect(formatUsdCents(-50000)).toBe('−$500.00');
  });

  it('shows a fraction as the percentage the terminal prints, and keeps the value returned', () => {
    expect(formatCell(0.851063829787234, 'fraction')).toBe('85.106%');
    expect(rawCell(0.851063829787234, 'fraction')).toBe('0.851063829787234');
    expect(formatCell(1360 / 777339, 'fraction')).toBe('0.175%');
    expect(rawCell(0.0022431396707766737, 'fraction')).toBe('0.0022431396707766737');
    expect(formatCell(1, 'fraction')).toBe('100.000%');
    expect(rawCell(1, 'fraction')).toBe('1');
    expect(formatCell(0, 'fraction')).toBe('0.000%');
    expect(rawTitle('0.75')).toBe('Value returned: 0.75');
    expect(formatPercent(0.4324324)).toBe('43.24%');
    expect(formatPercent(null)).toBe('—');
  });

  it('never shows a value outside 0 to 1 as a percentage, whatever the hint', () => {
    expect(formatCell(17.5, 'fraction')).toBe('17.5');
    expect(formatCell(486.44493116395495, 'fraction')).toBe('486.4449');
    expect(formatCell(-0.2, 'fraction')).toBe('-0.2');
    expect(formatCell(1.0001, 'fraction')).toBe('1.0001');
    expect(rawCell(17.5, 'fraction')).toBeUndefined();
    expect(rawCell(-0.2, 'fraction')).toBeUndefined();
  });

  it('shows a percentage only where the backend sent the fraction hint', () => {
    expect(formatCell(0.851063829787234, 'plain')).toBe('0.8511');
    expect(formatCell(486.44493116395495, 'plain')).toBe('486.4449');
    expect(formatCell(1, 'identifier')).toBe('1');
    expect(formatCell(50, 'usd_cents')).toBe('$0.50');
    expect(rawCell(0.5, 'plain')).toBeUndefined();
    expect(rawCell(0.5, 'usd_cents')).toBeUndefined();
    expect(rawCell(1, 'identifier')).toBeUndefined();
    expect(rawCell(null, 'fraction')).toBeUndefined();
    expect(rawCell('0.5', 'fraction')).toBeUndefined();
  });

  it('copies a table with the values as returned', () => {
    const table: TableResult = {
      columns: [
        { name: 'segment', format: 'plain' },
        { name: 'labelled', format: 'plain' },
        { name: 'fraud_rate', format: 'fraction' },
        { name: 'amount_usd_cents', format: 'usd_cents' },
      ],
      rows: [
        ['Rome, Italy', 1598, 0.851063829787234, -5000],
        ['a\tb\nc', 551905, null, 12],
      ],
      row_count: 2,
      truncated: false,
    };
    expect(tableAsTsv(table)).toBe(
      'segment\tlabelled\tfraud_rate\tamount_usd_cents\n' +
        'Rome, Italy\t1598\t0.851063829787234\t-5000\n' +
        'a\\tb\\nc\t551905\tNULL\t12',
    );
  });

  it('follows the column format hint', () => {
    expect(formatCell(null, 'plain')).toBe('NULL');
    expect(formatCell(7475327, 'identifier')).toBe('7475327');
    expect(formatCell(1159966, 'plain')).toBe('1,159,966');
    expect(formatCell(0.123456, 'plain')).toBe('0.1235');
    expect(formatCell(3.170634920634921, 'plain')).toBe('3.1706');
    expect(formatCell(0.000123456789, 'plain')).toBe('0.000123457');
    expect(formatCell(-5000, 'usd_cents')).toBe('−$50.00');
    expect(formatCell(0.5, 'fraction')).toBe('50.000%');
    expect(formatCell('Debit', 'usd_cents')).toBe('Debit');
  });

  it('formats a lift and a duration', () => {
    expect(formatTimes(22.51051)).toBe('22.5');
    expect(formatTimes(486.4449)).toBe('486.4');
    expect(formatSeconds(1234)).toBe('1.2 s');
  });
});

describe('text that code did not write', () => {
  it('makes hidden characters visible', () => {
    expect(safeText('first\nsecond\u001b[31m\u0000end')).toBe('first\\nsecond\\x1b[31m\\x00end');
    expect(safeText('a\tb\r')).toBe('a\\tb\\r');
    expect(safeText('user\u202egnp.exe')).toBe('user\\u202egnp.exe');
    expect(safeText('zero\u200bwidth\ufeff')).toBe('zero\\u200bwidth\\ufeff');
  });

  it('leaves ordinary text alone, markup included', () => {
    expect(safeText('<b>Rome</b> & “Italy” – café ✓')).toBe('<b>Rome</b> & “Italy” – café ✓');
  });

  it('cuts to a limit', () => {
    expect(safeText('abcdef', 4)).toBe('abc…');
    expect(safeText('abc', 4)).toBe('abc');
  });

  it('keeps line breaks and tabs in a block, and nothing else hidden', () => {
    expect(safeBlock('SELECT 1\r\nFROM\tt\u001b')).toBe('SELECT 1\nFROM\tt\\x1b');
  });
});
