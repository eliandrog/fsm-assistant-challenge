/**
 * A small SQL tokenizer for display only. It colours comments, strings,
 * numbers, keywords and function calls; everything else is plain text.
 * Not a parser: it never decides whether SQL is valid or safe.
 */

export type SqlTokenType = 'comment' | 'string' | 'number' | 'keyword' | 'function' | 'plain';

export interface SqlToken {
  type: SqlTokenType;
  text: string;
}

const KEYWORDS = new Set(
  `select from where and or not in is null as on join left right inner outer cross full group by
   order having limit offset distinct case when then else end between like glob exists union all
   intersect except with asc desc using cast over partition delete insert update drop alter create
   table into values set true false`
    .split(/\s+/)
    .filter(Boolean),
);

// Order matters: comments and strings first so their contents are not re-tokenized.
const TOKEN =
  /(--[^\n]*|\/\*[\s\S]*?\*\/)|('(?:[^']|'')*'|"(?:[^"]|"")*")|(\b\d+(?:\.\d+)?\b)|([A-Za-z_][A-Za-z0-9_]*)(\s*\()?/g;

export function tokenizeSql(sql: string): SqlToken[] {
  const tokens: SqlToken[] = [];
  const push = (type: SqlTokenType, text: string) => {
    if (!text) return;
    const last = tokens[tokens.length - 1];
    if (last && last.type === 'plain' && type === 'plain') last.text += text;
    else tokens.push({ type, text });
  };

  let cursor = 0;
  for (const match of sql.matchAll(TOKEN)) {
    const [whole, comment, string, number, word, paren] = match;
    const index = match.index;
    push('plain', sql.slice(cursor, index));

    if (comment) push('comment', comment);
    else if (string) push('string', string);
    else if (number) push('number', number);
    else if (word) {
      const isKeyword = KEYWORDS.has(word.toLowerCase());
      push(isKeyword ? 'keyword' : paren ? 'function' : 'plain', word);
      if (paren) push('plain', paren);
    }
    cursor = index + whole.length;
  }
  push('plain', sql.slice(cursor));
  return tokens;
}
