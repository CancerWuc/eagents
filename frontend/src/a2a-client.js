import { getSession } from './auth.js'
import { randomUuid } from './uuid.js'
import { buildA2aMessage } from './a2a-message.js'
import { parseA2aTask } from './a2a-response.js'
import { consumeA2aStream } from './a2a-stream.js'

export { buildA2aMessage } from './a2a-message.js'

export async function sendA2aMessage({ slug, text, contextId, taskId, inputs, services,
  interactionResponse, files = [], streaming = true, onProgress, signal, consolePage }) {
  const session = getSession()
  if (!session?.token) throw new Error('请先登录')
  const id = randomUuid()
  const { message, extension } = buildA2aMessage({ text, contextId, taskId, inputs, services, interactionResponse, files })
  const headers = { 'Content-Type': 'application/json', 'Authorization': `Bearer ${session.token}`, 'A2A-Version': '1.0' }
  if (consolePage) headers['X-EAgent-Console-Page'] = consolePage
  if (extension) headers['A2A-Extensions'] = extension
  if (streaming) headers.Accept = 'text/event-stream'
  const response = await fetch(`/a2a/${encodeURIComponent(slug)}/rpc`, {
    method: 'POST', headers, signal,
    body: JSON.stringify({ jsonrpc: '2.0', id,
      method: streaming ? 'SendStreamingMessage' : 'SendMessage', params: { message } }),
  })
  const { task } = await consumeA2aStream(response, id, { asUser: session.userId, onProgress })
  return parseA2aTask(task, session.userId)
}

async function taskRequest(slug, method, taskId, consolePage) {
  const session = getSession()
  if (!session?.token) throw new Error('请先登录')
  const id = randomUuid()
  const headers = { 'Content-Type': 'application/json',
    Authorization: `Bearer ${session.token}`, 'A2A-Version': '1.0' }
  if (consolePage) headers['X-EAgent-Console-Page'] = consolePage
  const response = await fetch(`/a2a/${encodeURIComponent(slug)}/rpc`, {
    method: 'POST', headers,
    body: JSON.stringify({ jsonrpc: '2.0', id, method, params: { id: taskId } }),
  })
  const envelope = await response.json()
  if (!response.ok || envelope.error) throw new Error(envelope.error?.message || `A2A HTTP ${response.status}`)
  if (envelope.id !== id) throw new Error('响应 ID 不匹配')
  return parseA2aTask(envelope.result?.task || envelope.result, session.userId)
}

export const getA2aTask = (slug, taskId, consolePage) => taskRequest(slug, 'GetTask', taskId, consolePage)
export const cancelA2aTask = (slug, taskId, consolePage) => taskRequest(slug, 'CancelTask', taskId, consolePage)

export async function reconnectA2aTask(slug, taskId, onProgress, consolePage) {
  const current = await getA2aTask(slug, taskId, consolePage)
  onProgress?.(current)
  if (!['SUBMITTED', 'WORKING'].includes(current.status)) return current
  const session = getSession()
  const id = randomUuid()
  const headers = { 'Content-Type': 'application/json',
    Authorization: `Bearer ${session.token}`, 'A2A-Version': '1.0', Accept: 'text/event-stream' }
  if (consolePage) headers['X-EAgent-Console-Page'] = consolePage
  const response = await fetch(`/a2a/${encodeURIComponent(slug)}/rpc`, {
    method: 'POST', headers,
    body: JSON.stringify({ jsonrpc: '2.0', id, method: 'SubscribeToTask', params: { id: taskId } }),
  })
  const { task } = await consumeA2aStream(response, id, { asUser: session.userId, onProgress })
  return parseA2aTask(task, session.userId)
}
