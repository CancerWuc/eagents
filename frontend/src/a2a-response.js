const CANVAS_CARD_MEDIA = 'application/vnd.eagent.hiagent-canvas-card+json'

export function parseA2aTask(task, asUser = '') {
  const state = task.status?.state
  const parts = task.status?.message?.parts || []
  const artifacts = task.artifacts || []
  const metadata = artifacts[0]?.metadata || {}
  const artifactParts = artifacts.flatMap(artifact => artifact.parts || [])
  return {
    taskId: task.id, contextId: task.contextId, asUser,
    status: state === 'TASK_STATE_COMPLETED' ? 'SUCCEEDED' : state?.replace('TASK_STATE_', ''),
    inputRequired: state === 'TASK_STATE_INPUT_REQUIRED',
    output: state === 'TASK_STATE_INPUT_REQUIRED' || state === 'TASK_STATE_FAILED'
      || state === 'TASK_STATE_CANCELED' || state === 'TASK_STATE_REJECTED'
      ? parts.map(part => part.text || '').join('')
      : artifactParts.map(part => part.text || '').join('') || parts.map(part => part.text || '').join(''),
    cards: artifactParts
      .filter(part => part.mediaType === CANVAS_CARD_MEDIA && part.data?.type === 'canvas_card')
      .map(part => part.data),
    interaction: parts.find(part => part.mediaType === 'application/vnd.eagent.hiagent-interaction+json')?.data,
    errorDetail: parts.find(part => part.mediaType === 'application/json')?.data?.code,
    runId: metadata.run_id || '', traceId: metadata.trace_id || '', routedServices: [], toolCalls: [],
  }
}
