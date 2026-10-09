import test from 'node:test'
import assert from 'node:assert/strict'

const baseline = process.env.FRONTEND_BASELINE_MODULE
const formatting = await import(baseline || '../src/format.js')
const rules = await import(baseline || '../src/form-rules.js')
const users = await import(baseline || '../src/permission-users.js')
const registration = await import(baseline || '../src/hiagent-registration.js')

test('date formatting preserves page-specific empty values and ISO precision', () => {
  for (const value of [null, undefined, '']) {
    assert.equal(formatting.fmtDateTime(value), '—')
    assert.equal(formatting.fmtDateTime(value, ''), '')
    assert.equal(formatting.fmtDateTime(value, '') || '刚刚', '刚刚')
  }
  assert.equal(formatting.fmtDateTime('2026-10-08T12:03:04.123456+08:00'), '2026-10-08 12:03:04')
  assert.equal(formatting.fmtDateTime('2026-10-08 12:03:04'), '2026-10-08 12:03:04')
})

test('slug and URL validation retain whitespace, optional values and exact errors', () => {
  const slugError = '2–64 位，小写字母或数字开头，仅含小写字母、数字与短横线'
  for (const value of ['', '  ', ' ab ', '0-test', 'a'.repeat(64)]) assert.equal(rules.slugValidationError(value), '')
  for (const value of ['a', 'Ab', '-ab', 'a_b', 'a'.repeat(65)]) assert.equal(rules.slugValidationError(value), slugError)
  for (const value of ['', ' ', ' HTTP://host ', 'https://host/path']) assert.equal(rules.httpUrlError(value), '')
  for (const value of ['host', 'ftp://host', 'http://']) assert.equal(rules.httpUrlError(value), '需以 http:// 或 https:// 开头')
})

test('permission candidates preserve order, active status, matching and exclusions', () => {
  const pool = [
    { userId: 'Alice', displayName: '小王', department: 'Finance', status: 'active' },
    { userId: 'Bob', displayName: '财务主管', department: 'Office', status: 'disabled' },
    { userId: 'Carol', displayName: '财务', department: null, status: 'active' },
    { userId: 'David', status: 'active' },
    ...Array.from({length: 10}, (_, i) => ({ userId: `Extra${i}`, status: 'active' })),
  ]
  const ids = rows => rows.map(row => row.cap_user_name)
  assert.deepEqual(ids(users.activeUserCandidates(pool, ' aLi ')), ['Alice'])
  assert.deepEqual(ids(users.activeUserCandidates(pool, 'FINANCE')), ['Alice'])
  assert.deepEqual(ids(users.activeUserCandidates(pool, '财务')), ['Carol'])
  assert.deepEqual(ids(users.activeUserCandidates(pool, '', { exclude: ['Alice', 'Carol'], limit: 3 })), ['David', 'Extra0', 'Extra1'])
  assert.equal(users.activeUserCandidates(pool, '', { limit: 8 }).length, 8)
  assert.equal(users.activeUserCandidates(pool, 'extra', { limit: 6 }).length, 6)
  assert.deepEqual(users.activeUserCandidates(pool, 'not-found'), [])
})

const row = overrides => ({ name: 'priority', type: 'string', required: false, description: '', options: '', testValue: '', ...overrides })
const build = (rows, schema) => registration.buildHiagentRegistration(rows, '  你好  ', schema)

test('registration enum parsing preserves original types, empty enum and extension data', () => {
  const originalEnum = [1, 2]
  const parameter = row({ options: '1\n2', originalOptions: '1\n2', originalProperty: { type: 'string', enum: originalEnum, custom: 'kept' } })
  assert.strictEqual(registration.parameterChoices(parameter), originalEnum)
  const restored = build([parameter], { title: 'original', additionalProperties: true })
  assert.deepEqual(restored.schema.properties.priority, { type: 'string', enum: [1, 2], custom: 'kept' })
  assert.equal(restored.schema.title, 'original')
  assert.equal(restored.schema.additionalProperties, true)
  assert.deepEqual(build([row({ originalOptions: '', originalProperty: { type: 'string', enum: [] } })]).schema.properties.priority.enum, [])
  assert.deepEqual(registration.parameterChoices(row({ options: ' P1，P2\nP1, P3  ' })), ['P1', 'P2', 'P3'])
  assert.deepEqual(build([row({ options: ' P1，P2\nP1, P3  ' })]).schema.properties.priority.enum, ['P1', 'P2', 'P3'])
  assert.equal('enum' in build([row({ options: ' , \n' })]).schema.properties.priority, false)
})

test('registration handles changed types and typed connection test values without changing payloads', () => {
  const changed = row({ options: '1\n2', originalOptions: '1\n2', originalProperty: { type: 'integer', enum: [1, 2], minimum: 0 }, testValue: '1' })
  assert.deepEqual(registration.parameterChoices(changed), [1, 2])
  assert.deepEqual(build([changed]).schema.properties.priority, { type: 'string', enum: ['1', '2'] })
  const result = build([
    row({ name: 'count', type: 'integer', testValue: '0', required: true }),
    row({ name: 'flag', type: 'boolean', testValue: 'false' }),
    row({ name: 'object', type: 'object', testValue: '{"a":1}' }),
    row({ name: 'list', type: 'array', testValue: '[1,2]' }),
  ])
  assert.equal(Object.getPrototypeOf(result.connectionTest.inputs), null)
  assert.deepEqual(JSON.parse(JSON.stringify(result.connectionTest)), { query: '你好', inputs: { count: 0, flag: false, object: {a: 1}, list: [1, 2] } })
  assert.deepEqual(result.schema.required, ['count'])
  assert.deepEqual(result.errors, {})
})
