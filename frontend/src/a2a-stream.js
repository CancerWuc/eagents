import { parseA2aTask } from './a2a-response.js'

const END_STATES = new Set([
  'TASK_STATE_COMPLETED', 'TASK_STATE_FAILED', 'TASK_STATE_CANCELED',
  'TASK_STATE_REJECTED', 'TASK_STATE_INPUT_REQUIRED', 'TASK_STATE_AUTH_REQUIRED',
])

function applyEvent(task, event) {
  if (event.task) return event.task
  if (!task) throw new Error('A2A 流缺少初始 Task')
  if (event.statusUpdate) task.status = event.statusUpdate.status
  if (event.artifactUpdate) {
    const update = event.artifactUpdate
    const artifact = update.artifact
    task.artifacts ||= []
    const index = task.artifacts.findIndex(item => item.artifactId === artifact.artifactId)
    if (update.append) {
      if (index < 0) throw new Error('A2A Artifact 追加前缺少首片段')
      task.artifacts[index] = {
        ...task.artifacts[index],
        parts: [...(task.artifacts[index].parts || []), ...(artifact.parts || [])],
        metadata: { ...(task.artifacts[index].metadata || {}), ...(artifact.metadata || {}) },
      }
    } else if (index >= 0) task.artifacts[index] = artifact
    else task.artifacts.push(artifact)
  }
  return task
}

export async function consumeA2aStream(response, requestId, { asUser = '', onProgress } = {}) {
  if (!response.headers.get('content-type')?.includes('text/event-stream')) {
    const envelope = await response.json()
    if (!response.ok || envelope.error) throw new Error(envelope.error?.message || `A2A HTTP ${response.status}`)
    if (envelope.id !== requestId) throw new Error('响应 ID 不匹配')
    if (!envelope.result?.task) throw new Error('缺少 A2A Task')
    return { task: envelope.result.task, raw: JSON.stringify(envelope) }
  }
  if (!response.body) throw new Error('A2A SSE 响应缺少正文')
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let pending = '', raw = '', task = null
  async function consumeFrame(frame) {
    const data = frame.split('\n').filter(line => line.startsWith('data:'))
      .map(line => line.slice(5).trimStart()).join('\n')
    if (!data) return
    const envelope = JSON.parse(data)
    if (envelope.id !== requestId) throw new Error('响应 ID 不匹配')
    if (envelope.error) throw new Error(envelope.error.message || 'A2A 流执行失败')
    task = applyEvent(task, envelope.result || {})
    onProgress?.(parseA2aTask(task, asUser), envelope.result || {})
  }
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      const text = decoder.decode(value, { stream: true })
      raw += text
      pending += text
      pending = pending.replaceAll('\r\n', '\n')
      let end
      while ((end = pending.indexOf('\n\n')) >= 0) {
        const frame = pending.slice(0, end)
        pending = pending.slice(end + 2)
        await consumeFrame(frame)
      }
    }
    pending += decoder.decode()
    pending = pending.replaceAll('\r\n', '\n')
    if (pending.trim()) await consumeFrame(pending)
  } finally { reader.releaseLock() }
  if (!task || !END_STATES.has(task.status?.state)) throw new Error('A2A 流未返回本轮结束状态')
  return { task, raw }
}
