// Generated from the API's pydantic models by src/fsm_assistant/typescript.py.
// Do not edit: run `uv run python -m fsm_assistant.typescript`.

export interface ApiError {
  code: 'invalid_request' | 'conversation_busy' | 'not_found' | 'method_not_allowed' | 'internal';
  message: string;
  fields: FieldProblem[];
}

export interface Caveat {
  code: 'truncated' | 'label_coverage' | 'zero_rows' | 'small_sample' | 'high_flag_rate' | 'weak_month' | 'period_drop' | 'single_segment';
  message: string;
}

export type Cell = string | number | null;

export interface ChatRequest {
  conversation_id: string;
  message: string;
}

export interface Column {
  name: string;
  format: 'plain' | 'usd_cents' | 'fraction' | 'identifier';
}

export interface DatabaseHealth {
  present: boolean;
  readable: boolean;
  schema_context: boolean;
}

export interface FieldProblem {
  field: string;
  problem: string;
}

export interface Health {
  status: 'ok' | 'degraded';
  mode: 'live' | 'offline_demo';
  database: DatabaseHealth;
  key_configured: boolean;
  model_id: string;
  prompt_version: string;
  tracing: TracingHealth;
  limits: Limits;
  evidence_note: string;
  demo_questions: string[];
}

export interface Limits {
  max_message_characters: number;
  row_cap: number;
}

export type ReasonCode = 'empty' | 'code_fence' | 'invalid_text' | 'too_long' | 'too_complex' | 'multiple_statements' | 'explain' | 'unterminated_quote' | 'sqlite_error' | 'not_read_only' | 'not_select' | 'table_not_allowed' | 'function_not_allowed' | 'relative_to_today' | 'clause_not_contained' | 'label_read' | 'not_deterministic';

export interface RuleCard {
  flagged: number;
  flagged_labelled: number;
  flagged_unlabelled: number;
  true_positives: number;
  false_positives: number;
  false_negatives: number;
  total_transactions: number;
  total_labelled: number;
  total_fraud: number;
  precision: number | null;
  recall: number | null;
  flag_rate: number | null;
  flagged_amount_cents: number;
  fraud_amount_caught_cents: number;
  false_positive_amount_cents: number;
  base_fraud_rate: number | null;
  lift: number | null;
  months: RuleMonth[];
  weakest_month: string | null;
  earlier: RulePeriod;
  later: RulePeriod;
  stability: RuleStability;
}

export interface RuleMonth {
  month: string;
  flagged: number;
  flagged_labelled: number;
  true_positives: number;
  false_positives: number;
  total_transactions: number;
  total_labelled: number;
  total_fraud: number;
  flagged_amount_cents: number;
  fraud_amount_caught_cents: number;
  false_positive_amount_cents: number;
  precision: number | null;
  recall: number | null;
  flag_rate: number | null;
}

export interface RulePeriod {
  first_month: string | null;
  last_month: string | null;
  months: number;
  flagged: number;
  flagged_labelled: number;
  true_positives: number;
  false_positives: number;
  total_transactions: number;
  total_labelled: number;
  total_fraud: number;
  precision: number | null;
  recall: number | null;
  lift: number | null;
}

export interface RuleStability {
  min_month_labelled: number;
  months: number;
  months_flagged: number;
  months_with_labelled_flags: number;
  qualifying_months: number;
  min_precision: number | null;
  max_precision: number | null;
}

export interface TableResult {
  columns: Column[];
  rows: Cell[][];
  row_count: number;
  truncated: boolean;
}

export interface TracingHealth {
  enabled: boolean;
  destination: string | null;
}

export interface TurnError {
  kind: 'sql_rejected' | 'sql_execution' | 'timeout' | 'invalid_input' | 'model_unavailable' | 'model_output' | 'internal';
  message: string;
  detail: string | null;
}

export interface TurnMeta {
  conversation_id: string;
  turn: number;
  model_id: string;
  prompt_version: string;
  tool_mode: 'tools' | 'structured';
  attempts: number;
  model_calls: number;
  input_tokens: number;
  output_tokens: number;
  elapsed_ms: number;
}

export interface TurnResult {
  outcome: 'answered' | 'rule_proposed' | 'clarification' | 'cannot_answer' | 'rejected' | 'failed';
  status: 'ok' | 'needs_clarification' | 'cannot_answer' | 'rejected' | 'error';
  error: TurnError | null;
  answer: string;
  answer_source: 'code' | 'model';
  tool: 'run_sql' | 'propose_rule' | 'profile_fraud' | 'ask_clarification' | 'decline' | null;
  refused: (ReasonCode | 'no_tool_call' | 'multiple_tool_calls' | 'unknown_tool' | 'invalid_arguments' | 'invalid_json' | 'cross_field')[];
  write_attempts: number;
  sql: string | null;
  where_clause: string | null;
  table: TableResult | null;
  rule: RuleCard | null;
  explanation: string | null;
  assumptions: string[];
  suggestions: string[];
  caveats: Caveat[];
  meta: TurnMeta;
  notes: string[];
}
