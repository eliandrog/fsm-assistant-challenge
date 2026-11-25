// Generated from the API's pydantic models by src/fsm_assistant/typescript.py.
// Do not edit: run `uv run python -m fsm_assistant.typescript`.

export interface ApiError {
  code: 'invalid_request' | 'conversation_busy' | 'not_found' | 'method_not_allowed' | 'internal' | 'finder_busy' | 'finder_unavailable' | 'not_reviewable' | 'not_processable';
  message: string;
  fields: FieldProblem[];
}

export interface BankEntry {
  run_id: number;
  position: number;
  title: string;
  hypothesis: string;
  columns: string[];
  scenario_assumptions: string[];
  status: 'confirmed' | 'not_confirmed' | 'no_signal' | 'not_built' | 'proposed' | 'discarded';
  turn_outcome: 'answered' | 'rule_proposed' | 'clarification' | 'cannot_answer' | 'rejected' | 'failed' | null;
  turn_message: string | null;
  refused: string[];
  clause: string | null;
  explanation: string | null;
  rule_assumptions: string[];
  rule: RuleCard | null;
  caveats: Caveat[];
  criteria: Criterion[];
  duplicate_check: 'complete' | 'incomplete' | 'not_applicable';
  model_id: string;
  scenario_prompt_version: string;
  rule_prompt_version: string;
  processed_run_id: number | null;
  turn_error_kind: string | null;
  id: number;
  created_at: string;
  decision: 'pending' | 'approved' | 'rejected';
  decision_note: string | null;
  decided_at: string | null;
  matches: ScenarioMatch[];
  backtest_sql: string | null;
  monthly_sql: string | null;
  verdict: string;
  verdict_short: string;
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

export interface Criteria {
  confirmation_starts: string;
  discovery_min_labelled: number;
  discovery_min_fraud: number;
  discovery_min_lift: number;
  confirmation_min_labelled: number;
  confirmation_min_fraud: number;
  confirmation_min_lift: number;
  confirmation_lift_ratio: number;
}

export interface Criterion {
  code: string;
  period: 'discovery' | 'confirmation';
  passed: boolean;
  detail: string;
}

export interface DatabaseHealth {
  present: boolean;
  readable: boolean;
  schema_context: boolean;
}

export interface DecisionRequest {
  decision: 'pending' | 'approved' | 'rejected';
  note?: string | null;
}

export interface EntrySummary {
  id: number;
  run_id: number;
  title: string;
  status: 'confirmed' | 'not_confirmed' | 'no_signal' | 'not_built' | 'proposed' | 'discarded';
  decision: 'pending' | 'approved' | 'rejected';
  turn_outcome: 'answered' | 'rule_proposed' | 'clarification' | 'cannot_answer' | 'rejected' | 'failed' | null;
  discovery: RulePeriod | null;
  confirmation: RulePeriod | null;
  verdict_short: string;
  matches: number;
  created_at: string;
}

export interface FieldProblem {
  field: string;
  problem: string;
}

export interface FinderOverview {
  running: boolean;
  active: RunProgress | null;
  propose_requests: number;
  process_requests: number;
  criteria: Criteria;
  note: string;
  last_run: FinderRun | null;
  proposed: ProposedScenario[];
  discarded: number;
  entries: EntrySummary[];
}

export interface FinderRun {
  id: number | null;
  kind: 'propose' | 'process' | 'batch';
  status: 'running' | 'completed' | 'stopped' | 'failed' | 'interrupted';
  error: RunError | null;
  source: 'live' | 'offline_demo' | 'scripted';
  batch_size: number;
  scenarios: number;
  items: RunItem[];
  model_requests: number;
  request_bound: number;
  input_tokens: number;
  output_tokens: number;
  elapsed_ms: number;
  model_id: string;
  scenario_prompt_version: string;
  rule_prompt_version: string;
  started_at: string;
  finished_at: string | null;
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

export interface ProposeRequest {
  count?: number;
}

export interface ProposedScenario {
  id: number;
  run_id: number;
  title: string;
  hypothesis: string;
  columns: string[];
  assumptions: string[];
  created_at: string;
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

export interface RunError {
  kind: 'bank_unavailable' | 'database' | 'model_unavailable' | 'model_output' | 'scenario_shape' | 'demo_exhausted' | 'interrupted' | 'internal';
  message: string;
}

export interface RunItem {
  position: number;
  title: string;
  result: 'proposed' | 'added' | 'duplicate';
  entry_id: number;
  status: 'confirmed' | 'not_confirmed' | 'no_signal' | 'not_built' | 'proposed' | 'discarded';
  match_kind: 'same_clause' | 'same_transactions' | null;
}

export interface RunProgress {
  run_id: number;
  entry_id: number;
  state: 'running' | 'completed' | 'stopped' | 'failed' | 'interrupted';
  step: 'write_rule' | 'check_rule' | 'backtest' | 'duplicates' | 'verdict' | null;
  done: ('write_rule' | 'check_rule' | 'backtest' | 'duplicates' | 'verdict')[];
  attempt: number;
  scenario: Scenario;
  clause: string | null;
  outcome: 'added' | 'duplicate' | null;
  result_entry_id: number | null;
  status: 'confirmed' | 'not_confirmed' | 'no_signal' | 'not_built' | 'proposed' | 'discarded' | null;
  match_kind: 'same_clause' | 'same_transactions' | null;
  error: RunError | null;
  verdict: string | null;
}

export interface Scenario {
  title: string;
  hypothesis: string;
  columns: string[];
  assumptions: string[];
}

export interface ScenarioMatch {
  run_id: number;
  position: number;
  title: string;
  hypothesis: string;
  clause: string;
  kind: 'same_clause' | 'same_transactions';
  created_at: string;
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
