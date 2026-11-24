/**
 * Real response bodies of the API, written by the backend's test
 * tests/test_web_contract.py, which fails when the API stops returning them.
 */
import type { ApiError, Health, TurnResult } from '../api/types';
import answeredAfterWrite from './fixtures/answered_after_write.json';
import answeredEmpty from './fixtures/answered_empty.json';
import answeredHostile from './fixtures/answered_hostile.json';
import answeredScalar from './fixtures/answered_scalar.json';
import answeredTable from './fixtures/answered_table.json';
import answeredTruncated from './fixtures/answered_truncated.json';
import apiErrorInvalid from './fixtures/api_error_invalid.json';
import cannotAnswer from './fixtures/cannot_answer.json';
import clarification from './fixtures/clarification.json';
import failedModel from './fixtures/failed_model.json';
import failedSql from './fixtures/failed_sql.json';
import healthDemo from './fixtures/health_demo.json';
import healthLive from './fixtures/health_live.json';
import healthNoDatabase from './fixtures/health_no_database.json';
import healthNoKey from './fixtures/health_no_key.json';
import rejected from './fixtures/rejected.json';
import rejectedWrite from './fixtures/rejected_write.json';
import ruleProposed from './fixtures/rule_proposed.json';
import scan from './fixtures/scan.json';

// JSON modules type a literal as `string`; the backend's test vouches for the shapes.
const turn = (body: unknown) => body as TurnResult;
const health = (body: unknown) => body as Health;

export const TURNS = {
  answeredScalar: turn(answeredScalar),
  answeredTable: turn(answeredTable),
  answeredTruncated: turn(answeredTruncated),
  answeredEmpty: turn(answeredEmpty),
  answeredHostile: turn(answeredHostile),
  ruleProposed: turn(ruleProposed),
  scan: turn(scan),
  clarification: turn(clarification),
  cannotAnswer: turn(cannotAnswer),
  rejected: turn(rejected),
  rejectedWrite: turn(rejectedWrite),
  answeredAfterWrite: turn(answeredAfterWrite),
  failedSql: turn(failedSql),
  failedModel: turn(failedModel),
};

export const HEALTH = {
  live: health(healthLive),
  noKey: health(healthNoKey),
  demo: health(healthDemo),
  noDatabase: health(healthNoDatabase),
};

export const API_ERROR_INVALID = apiErrorInvalid as ApiError;
