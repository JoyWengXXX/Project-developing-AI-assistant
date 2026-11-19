import assert from 'node:assert/strict'
import { test } from 'node:test'
import { qaErrorText, QA_ERROR_HINT } from '../src/qaErrors.ts'
import { durationText, sourceWindow } from '../src/qaQuestion.ts'

test('token_budget_exceeded names RepoPilot quota, the reason and the numbers', () => {
  const text = qaErrorText({
    code: 'token_budget_exceeded',
    message: 'The token budget does not allow this request.',
    details: { reason: 'daily_tokens', limit: 1000, requested: 4096, used: 900 },
  })
  assert.match(text.summary, /RepoPilot/)
  assert.match(text.summary, /每日 token 上限/)
  assert.match(text.summary, /不是供應商的限制/)
  assert.match(text.summary, /4,096/)
})

test('token_budget_exceeded after a failed run only has the reason in the message', () => {
  const text = qaErrorText({
    code: 'token_budget_exceeded',
    message: 'The token budget does not allow this request (per_request_tokens).',
  })
  assert.match(text.summary, /單次請求 token 上限/)
})

test('qa_egress_not_consented names every repository', () => {
  const text = qaErrorText({
    code: 'qa_egress_not_consented',
    message: 'x',
    details: { repositories: [{ repository_id: 1, name: 'alpha' }, { repository_id: 2, name: 'beta' }] },
  })
  assert.match(text.summary, /「alpha」、「beta」/)
  assert.match(text.advice ?? '', /同意/)
})

test('provider_spend_limit_reached says it is a limit on the provider side', () => {
  const text = qaErrorText({ code: 'provider_spend_limit_reached', message: 'x' })
  assert.match(text.summary, /供應商端/)
  assert.match(text.summary, /不是 RepoPilot/)
})

test('unknown codes fall back to the backend message; hostile details are ignored', () => {
  assert.equal(qaErrorText({ code: 'something_new', message: 'Backend says hi' }).summary, 'Backend says hi')
  const text = qaErrorText({
    code: 'qa_egress_not_consented',
    message: 'x',
    details: { repositories: [{ name: { evil: true } }, 'str', null] },
  })
  assert.match(text.summary, /部分已選取的 repo/)
})

test('every failure family has a Traditional-Chinese message', () => {
  for (const code of [
    'provider_max_tokens', 'provider_refusal', 'provider_context_window_exceeded', 'provider_timeout',
    'provider_rate_limited', 'provider_unavailable', 'provider_payment_required', 'provider_request_too_large',
    'provider_malformed_response', 'provider_auth_failed', 'provider_bad_request', 'provider_spend_limit_reached',
    'qa_model_unsupported', 'answer_parse_error', 'answer_too_large', 'answer_not_json', 'answer_not_object',
    'answer_unexpected_fields', 'answer_invalid_field', 'answer_empty_statements',
  ]) {
    assert.ok(QA_ERROR_HINT[code], code)
  }
})

test('sourceWindow clamps to the file and keeps the cited range inside', () => {
  assert.deepEqual(sourceWindow(100, 50, 52, 5), { from: 45, to: 57 })
  assert.deepEqual(sourceWindow(10, 1, 2, 5), { from: 1, to: 7 })
  assert.deepEqual(sourceWindow(10, 9, 30, 5), { from: 4, to: 10 })
  assert.deepEqual(sourceWindow(3, 8, 9, 5), { from: 1, to: 3 })
})

test('durationText', () => {
  assert.equal(durationText(null), '—')
  assert.equal(durationText(850), '850 毫秒')
  assert.equal(durationText(12_300), '12.3 秒')
  assert.equal(durationText(125_000), '2 分 5 秒')
})
