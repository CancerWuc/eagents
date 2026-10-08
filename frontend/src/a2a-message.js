import { randomUuid } from './uuid.js'

function filePart(file, index) {
  if (!file || typeof file !== 'object' || Array.isArray(file)) throw new Error(`文件 ${index + 1} 必须是对象`)
  let parsed
  try { parsed = new URL(file.url) } catch { throw new Error(`文件 ${index + 1} 的 url 必须是 HTTPS 地址`) }
  if (parsed.protocol !== 'https:' || !parsed.hostname || parsed.username || parsed.password) throw new Error(`文件 ${index + 1} 的 url 必须是无用户信息的 HTTPS 地址`)
  if (typeof file.filename !== 'string' || !file.filename.trim()) throw new Error(`文件 ${index + 1} 的 filename 不能为空`)
  if (typeof file.mediaType !== 'string' || !file.mediaType.trim()) throw new Error(`文件 ${index + 1} 的 mediaType 不能为空`)
  if (typeof file.path !== 'string' || !file.path.trim()) throw new Error(`文件 ${index + 1} 的 path 不能为空`)
  const pathParts = file.path.replaceAll('\\', '/').split('/')
  if (file.path.startsWith('/') || file.path.startsWith('\\') || /^[A-Za-z]:[\\/]/.test(file.path) || pathParts.includes('..')) throw new Error(`文件 ${index + 1} 的 path 必须是无 .. 的相对路径`)
  if (!Number.isInteger(file.size) || file.size < 0) throw new Error(`文件 ${index + 1} 的 size 必须是非负整数`)
  return { url: file.url, filename: file.filename, mediaType: file.mediaType,
    metadata: { hiagent: { path: file.path, size: file.size } } }
}

export function buildA2aMessage({ text, contextId, taskId, inputs, services, interactionResponse, files = [] }) {
  if (!Array.isArray(files)) throw new Error('files 必须是数组')
  if (files.length && services !== undefined) throw new Error('Agent Team 不支持 files')
  if (files.length && inputs === undefined) throw new Error('files 仅支持 HiAgent 直连 inputs 模式')
  const message = { messageId: randomUuid(), role: 'ROLE_USER', parts: [{ text, mediaType: 'text/plain' }] }
  if (contextId) message.contextId = contextId
  if (taskId) message.taskId = taskId
  if (interactionResponse) message.parts = [{ data: interactionResponse, mediaType: 'application/vnd.eagent.hiagent-interaction-response+json' }]
  let extension
  if (inputs !== undefined || services !== undefined) {
    const direct = inputs !== undefined
    extension = `https://eagent.example/extensions/${direct ? 'hiagent-inputs' : 'hiagent-services'}/v1`
    message.extensions = [extension]
    message.parts.push({ data: direct ? { inputs } : { services }, mediaType: `application/vnd.eagent.${direct ? 'hiagent-inputs' : 'hiagent-services'}+json` })
  }
  message.parts.push(...files.map(filePart))
  return { message, extension }
}
