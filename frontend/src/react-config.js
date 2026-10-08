export const RESPONSE_MODE_SYNTHESIZE = 'SYNTHESIZE'
export const RESPONSE_MODE_SINGLE_TOOL_RAW = 'SINGLE_TOOL_RAW'

export function parseReactConfig(raw) {
  const config = JSON.parse(raw || '{}')
  return {
    maxIters: config.max_iters ?? 8,
    responseMode: config.response_mode === RESPONSE_MODE_SINGLE_TOOL_RAW
      ? RESPONSE_MODE_SINGLE_TOOL_RAW
      : RESPONSE_MODE_SYNTHESIZE,
  }
}

export function reactConfigRequest(maxIters, responseMode) {
  return { maxIters, responseMode }
}
