import { tokenizeSql } from './highlightSql';

describe('tokenizeSql', () => {
  it('reproduces the input exactly', () => {
    const sql = "SELECT COUNT(*) AS n -- total\nFROM transactions WHERE card_type = 'Debit (Prepaid)' AND amount_usd_cents > 100000;";
    expect(tokenizeSql(sql).map((t) => t.text).join('')).toBe(sql);
  });

  it('classifies keywords, functions, strings, numbers and comments', () => {
    const tokens = tokenizeSql("select COUNT(*) from t where x = 'select' and y > 12.5 -- from");
    const of = (type: string) => tokens.filter((t) => t.type === type).map((t) => t.text);
    expect(of('keyword')).toEqual(['select', 'from', 'where', 'and']);
    expect(of('function')).toEqual(['COUNT']);
    expect(of('string')).toEqual(["'select'"]);
    expect(of('number')).toEqual(['12.5']);
    expect(of('comment')).toEqual(['-- from']);
  });
});
