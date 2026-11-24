import type { Health, TurnResult } from './types';

/**
 * Structural checks for JSON this interface did not write: a response body,
 * or a conversation read back from localStorage. They check what rendering
 * relies on, so a body of another shape becomes an error row, not a crash.
 */

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isStrings(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === 'string');
}

function isNullable(value: unknown, check: (value: unknown) => boolean): boolean {
  return value === null || check(value);
}

const isString = (value: unknown) => typeof value === 'string';

function isTable(value: unknown): boolean {
  return (
    isRecord(value) &&
    Array.isArray(value.columns) &&
    value.columns.every((c) => isRecord(c) && isString(c.name) && isString(c.format)) &&
    Array.isArray(value.rows) &&
    value.rows.every(Array.isArray) &&
    typeof value.row_count === 'number' &&
    typeof value.truncated === 'boolean'
  );
}

function isRule(value: unknown): boolean {
  return (
    isRecord(value) &&
    typeof value.flagged === 'number' &&
    Array.isArray(value.months) &&
    value.months.every((m) => isRecord(m) && isString(m.month)) &&
    isRecord(value.earlier) &&
    isRecord(value.later) &&
    isRecord(value.stability)
  );
}

export function isTurnResult(value: unknown): value is TurnResult {
  if (!isRecord(value)) return false;
  const { meta } = value;
  return (
    isString(value.outcome) &&
    isString(value.status) &&
    isString(value.answer) &&
    isNullable(value.error, (e) => isRecord(e) && isString(e.kind) && isString(e.message)) &&
    isNullable(value.tool, isString) &&
    isStrings(value.refused) &&
    isNullable(value.sql, isString) &&
    isNullable(value.where_clause, isString) &&
    isNullable(value.table, isTable) &&
    isNullable(value.rule, isRule) &&
    isNullable(value.explanation, isString) &&
    isStrings(value.assumptions) &&
    isStrings(value.suggestions) &&
    isStrings(value.notes) &&
    Array.isArray(value.caveats) &&
    value.caveats.every((c) => isRecord(c) && isString(c.code) && isString(c.message)) &&
    isRecord(meta) &&
    isString(meta.model_id) &&
    ['turn', 'model_calls', 'input_tokens', 'output_tokens', 'elapsed_ms'].every(
      (name) => typeof meta[name] === 'number',
    )
  );
}

export function isHealth(value: unknown): value is Health {
  return (
    isRecord(value) &&
    isString(value.status) &&
    isString(value.mode) &&
    isString(value.model_id) &&
    typeof value.key_configured === 'boolean' &&
    isRecord(value.database) &&
    isRecord(value.tracing) &&
    isRecord(value.limits) &&
    typeof value.limits.max_message_characters === 'number' &&
    isStrings(value.demo_questions)
  );
}
