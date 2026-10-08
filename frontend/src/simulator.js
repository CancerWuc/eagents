import { buildA2aMessage } from './a2a-message.js'
import { parseA2aTask } from './a2a-response.js'
import { consumeA2aStream } from './a2a-stream.js'
import { randomUuid } from './uuid.js'

export const INPUTS_URI = 'https://eagent.example/extensions/hiagent-inputs/v1'
export const SERVICES_URI = 'https://eagent.example/extensions/hiagent-services/v1'
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value)
const own = (value, key) => Object.hasOwn(value, key)

export function readContract(card, kind) {
  if (!card.supportedInterfaces?.some(i => i.protocolBinding === 'JSONRPC' && i.protocolVersion === '1.0')) {
    throw new Error('目标未声明 A2A 1.0 JSONRPC 接口')
  }
  const uri = kind === 'MASTER' ? SERVICES_URI : INPUTS_URI
  const extensions = card.capabilities?.extensions || []
  if (extensions.some(e => e.required && ![uri].includes(e.uri))) throw new Error('目标包含不支持的必需扩展')
  const extension = extensions.find(e => e.uri === uri)
  const media = kind === 'MASTER' ? 'application/vnd.eagent.hiagent-services+json' : 'application/vnd.eagent.hiagent-inputs+json'
  if (!extension || extension.params?.mediaType !== media) throw new Error('缺少或不支持参数扩展，无法生成请求')
  if (kind === 'MASTER') {
    if (!object(extension.params.services) || Object.values(extension.params.services).some(s => !object(s))) throw new Error('服务参数 Schema 无效')
    return { kind, uri, media, schemas: extension.params.services }
  }
  if (!object(extension.params.schema)) throw new Error('Inputs Schema 无效')
  return { kind, uri, media, schemas: { inputs: extension.params.schema } }
}

export function fieldType(schema) {
  if (!object(schema) || ['$ref', '$dynamicRef', 'oneOf', 'anyOf', 'allOf', 'if', 'not', 'prefixItems'].some(k => own(schema, k))) return 'json'
  if (schema.enum) return 'enum'
  const types = Array.isArray(schema.type) ? schema.type.filter(t => t !== 'null') : [schema.type]
  if (types.includes('object') && (schema.patternProperties || object(schema.additionalProperties) || (!Object.keys(schema.properties || {}).length && schema.additionalProperties !== false))) return 'json'
  return types.length === 1 && ['object', 'array', 'string', 'boolean', 'number', 'integer'].includes(types[0]) ? types[0] : 'json'
}

// Explicit presence belongs to the parent object. No default values are injected.
export function initialValue(schema) {
  const type = fieldType(schema)
  if (type === 'object') return {}
  if (type === 'array') return []
  if (type === 'boolean') return false
  if (type === 'number' || type === 'integer') return 0
  if (type === 'enum') return schema.enum[0]
  if (type === 'json') return null
  return ''
}

export function validateBasic(value, schema, path = '参数') {
  const errors = []
  if (!object(schema)) return errors // Complex schema is ultimately validated by the server.
  if (Array.isArray(schema.enum) && !schema.enum.some(v => JSON.stringify(v) === JSON.stringify(value))) errors.push(`${path}：请选择允许的值`)
  const types = Array.isArray(schema.type) ? schema.type : schema.type ? [schema.type] : []
  const matches = type => ({ null: value === null, object: object(value), array: Array.isArray(value),
    string: typeof value === 'string', boolean: typeof value === 'boolean',
    number: typeof value === 'number' && Number.isFinite(value), integer: Number.isSafeInteger(value) })[type]
  if (types.length && !types.some(matches)) return [...errors, `${path}：类型应为 ${types.join(' / ')}`]
  if (typeof value === 'string' && schema.minLength !== undefined && [...value].length < schema.minLength) errors.push(`${path}：至少 ${schema.minLength} 个字符`)
  if (typeof value === 'number') {
    if (schema.minimum !== undefined && value < schema.minimum) errors.push(`${path}：不能小于 ${schema.minimum}`)
    if (schema.maximum !== undefined && value > schema.maximum) errors.push(`${path}：不能大于 ${schema.maximum}`)
  }
  if (object(value)) {
    for (const key of schema.required || []) if (!own(value, key)) errors.push(`${path}.${key}：必填`)
    for (const [key, child] of Object.entries(value)) {
      if (own(schema.properties || {}, key)) errors.push(...validateBasic(child, schema.properties[key], `${path}.${key}`))
      else if (schema.additionalProperties === false && !schema.patternProperties) errors.push(`${path}.${key}：未声明的字段`)
    }
  }
  if (Array.isArray(value) && object(schema.items)) value.forEach((v, i) => errors.push(...validateBasic(v, schema.items, `${path}[${i}]`)))
  return errors
}

export function validateParameters(contract, values) {
  if (!object(values)) return ['参数必须为 JSON 对象']
  if (contract.kind !== 'MASTER') return validateBasic(values, contract.schemas.inputs)
  return Object.entries(values).flatMap(([slug, entry]) => {
    if (!own(contract.schemas, slug)) return [`未知服务：${slug}`]
    if (!object(entry) || Object.keys(entry).length !== 1 || !own(entry, 'inputs')) return [`${slug}：应包含 inputs 对象`]
    if (!object(entry.inputs)) return [`${slug}：inputs 必须为对象`]
    return validateBasic(entry.inputs, contract.schemas[slug], slug)
  })
}

export function buildSimulationRequest({ contract, values, text, contextId, pending, optionId, streaming = true }) {
  const errors = validateParameters(contract, values)
  if (errors.length) throw new Error(errors.join('；'))
  const resume = contract.kind === 'HIAGENT' && pending?.inputRequired
  if (resume && (!optionId || !pending.interaction?.options?.some(o => o.id === optionId))) throw new Error('请选择有效的交互选项')
  if (!resume && !text.trim()) throw new Error('请输入消息')
  const { message, extension } = buildA2aMessage({ text, contextId,
    ...(contract.kind === 'MASTER' ? (Object.keys(values).length ? { services: values } : {}) : { inputs: values }),
    ...(resume ? { taskId: pending.taskId, interactionResponse: { interactionId: pending.interaction.interactionId, selectedOptionIds: [optionId] } } : {}),
  })
  return { body: { jsonrpc: '2.0', id: randomUuid(), method: streaming ? 'SendStreamingMessage' : 'SendMessage', params: { message } },
    headers: { 'Content-Type': 'application/json', 'A2A-Version': '1.0', ...(streaming ? { Accept: 'text/event-stream' } : {}), ...(extension ? { 'A2A-Extensions': extension } : {}) } }
}

export async function sendSimulation({ base = '', slug, asUser, source, token, request, fetcher = fetch, onProgress }) {
  if (!token) throw new Error('请使用管理员 Session 登录')
  const started = performance.now()
  const response = await fetcher(`${base}/api/v1/admin/simulator/${encodeURIComponent(slug)}/rpc?${new URLSearchParams({ asUser, source })}`, {
    method: 'POST', headers: { ...request.headers, Authorization: `Bearer ${token}` }, body: JSON.stringify(request.body),
  })
  let streamResult
  if (request.body.method === 'SendStreamingMessage' && response.ok && response.headers.get('content-type')?.includes('text/event-stream')) {
    try { streamResult = await consumeA2aStream(response, request.body.id, { asUser, onProgress }) }
    catch (error) {
      return { error: error.message, raw: '', httpStatus: response.status,
        elapsedMs: Math.round(performance.now() - started),
        traceId: response.headers.get('X-Simulation-Trace-Id') || '', simulationId: response.headers.get('X-Simulation-Id') || '' }
    }
  }
  const raw = streamResult?.raw ?? await response.text()
  let envelope
  try { envelope = JSON.parse(raw) } catch { /* Keep raw non-JSON errors. */ }
  const result = { raw, httpStatus: response.status, elapsedMs: Math.round(performance.now() - started),
    traceId: response.headers.get('X-Simulation-Trace-Id') || response.headers.get('X-Trace-Id') || '',
    simulationId: response.headers.get('X-Simulation-Id') || '', auditWarning: response.headers.get('X-Simulation-Audit-Warning') || '', envelope }
  if (!response.ok || envelope?.error) result.error = envelope?.error?.message || envelope?.message || `HTTP ${response.status}`
  else if (streamResult?.task) result.task = parseA2aTask(streamResult.task, asUser)
  else if (envelope?.id !== request.body.id) result.error = '响应 ID 不匹配'
  else if (!(envelope?.result?.task || envelope?.result?.status)) result.error = '响应缺少 A2A Task'
  else {
    try { result.task = parseA2aTask(envelope.result.task || envelope.result, asUser) }
    catch { result.error = '无法解析 A2A Task，请查看原始响应' }
  }
  if (envelope?.error?.code === -32001) result.error = `${result.error}；会话不可访问，请新建对话`
  return result
}

export function simulationTaskRequest(method, taskId) {
  return { body: { jsonrpc: '2.0', id: randomUuid(), method, params: { id: taskId } },
    headers: { 'Content-Type': 'application/json', 'A2A-Version': '1.0',
      ...(method === 'SubscribeToTask' ? { Accept: 'text/event-stream' } : {}) } }
}

export async function reconnectSimulation(args, taskId, onProgress) {
  const current = await sendSimulation({ ...args, request: simulationTaskRequest('GetTask', taskId) })
  if (current.error) throw new Error(current.error)
  onProgress?.(current.task)
  if (!['SUBMITTED', 'WORKING'].includes(current.task.status)) return current.task
  const resumed = await sendSimulation({ ...args, request: simulationTaskRequest('SubscribeToTask', taskId), onProgress })
  if (resumed.error) throw new Error(resumed.error)
  return resumed.task
}

export function formatSimulationResponse(raw) {
  if (!raw) return ''
  try { return JSON.stringify(JSON.parse(raw), null, 2) }
  catch { return raw }
}

export function simulationCurl({ url, request }) {
  const quote = value => "'" + value.replaceAll("'", "'\\''") + "'"
  // Payload may contain business secrets. Export the protocol shape, not form values.
  const sample = JSON.parse(JSON.stringify(request.body))
  sample.id = '<request-id>'
  sample.params.message.messageId = '<message-id>'
  delete sample.params.message.contextId
  delete sample.params.message.taskId
  sample.params.message.parts = sample.params.message.parts.map(part => {
    if ('text' in part) return { ...part, text: '<用户消息>' }
    if (part.mediaType === 'application/vnd.eagent.hiagent-inputs+json') return { ...part, data: { inputs: { '<字段名>': '<按 Schema 填写>' } } }
    if (part.mediaType === 'application/vnd.eagent.hiagent-services+json') return { ...part, data: { services: Object.fromEntries(Object.keys(part.data.services).map(slug => [slug, { inputs: { '<字段名>': '<按 Schema 填写>' } }])) } }
    return { ...part, data: { interactionId: '<interaction-id>', selectedOptionIds: ['<option-id>'] } }
  })
  return ['# 示例：经可信入口注入工号；请替换占位符后发送，交互恢复另需 taskId/contextId。',
    `curl -X POST ${quote(url)} \\`, ...Object.entries(request.headers).map(([k, v]) => `  -H ${quote(`${k}: ${v}`)} \\`),
    "  -H 'Authorization: Bearer <可信入口凭证>' \\",
    "  -H 'cap-user-name: <由可信入口注入的已登记工号>' \\", `  -d ${quote(JSON.stringify(sample, null, 2))}`].join('\n')
}
