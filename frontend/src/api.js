// 管理 API 封装: 身份经 cap-user-name header 传递；管理面鉴权双轨
// （admin-auth 01）：登录会话走 Authorization: Bearer，兼容通道 X-Admin-Token
// 错误响应为平台统一结构 {code, message, traceId}（兼容旧 detail 字段）
// E1: 非统一结构错误自动弹全局通知（含 code/traceId），供排障
import { promptText } from './ui.js'
import { config } from './config.js'
import { notify } from './notify.js'
import { clearSession, getSession } from './auth.js'

const USER = localStorage.getItem('eagent-user') || 'admin'
const BASE = config.apiBase

function adminToken() {
  return localStorage.getItem('eagent-admin-token') || ''
}

// 双轨鉴权头：登录会话优先（Bearer），否则旧管理令牌
function authHeaders() {
  const session = getSession()
  if (session?.token) {
    return {
      authorization: `Bearer ${session.token}`,
      'cap-user-name': session.userId || USER,
    }
  }
  return { 'cap-user-name': USER, 'x-admin-token': adminToken() }
}

async function readErrorBody(resp) {
  let body = {}
  try { body = await resp.json() } catch { /* keep */ }
  return body
}

async function errorMessage(resp) {
  const body = await readErrorBody(resp)
  return body.message || body.detail || resp.statusText
}

async function req(method, url, body, retried = false) {
  const resp = await fetch(BASE + url, {
    method,
    headers: {
      'content-type': 'application/json',
      ...authHeaders(),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (resp.status === 401 && !retried && !url.includes('/a2a/')) {
    // 会话过期/失效: 清掉回登录页；旧令牌通道维持弹窗重试
    if (getSession()?.token) {
      clearSession()
      window.location.reload()
      throw new Error('登录已过期')
    }
    // 管理面令牌缺失/失效: 页面内弹窗输入后重试一次
    const message = await errorMessage(resp.clone())
    if (String(message).includes('令牌')) {
      const token = await promptText('管理令牌无效或缺失，请输入 X-Admin-Token（由运维发放）')
      if (token) {
        localStorage.setItem('eagent-admin-token', token.trim())
        return req(method, url, body, true)
      }
    }
  }
  if (!resp.ok) {
    const errBody = await readErrorBody(resp)
    const err = new Error(errBody.message || errBody.detail || resp.statusText)
    err.code = errBody.code
    err.traceId = errBody.traceId
    notify({
      code: err.code,
      message: err.message,
      traceId: err.traceId,
    })
    if (resp.status === 403) window.dispatchEvent(new Event('eagent-forbidden'))
    throw err
  }
  return resp.json()
}

export const api = {
  user: USER,
  get: (url) => req('GET', url),
  post: (url, body) => req('POST', url, body ?? {}),
  put: (url, body) => req('PUT', url, body),
  del: (url) => req('DELETE', url),
  upload: async (url, file) => {
    // multipart 上传（不设 content-type，由浏览器生成 boundary）
    const resp = await fetch(BASE + url, {
      method: 'POST',
      headers: authHeaders(),
      body: (() => {
        const fd = new FormData()
        fd.append('file', file, file.name)
        return fd
      })(),
    })
    if (!resp.ok) {
      const errBody = await readErrorBody(resp)
      const err = new Error(errBody.message || resp.statusText)
      err.code = errBody.code
      err.traceId = errBody.traceId
      notify({ code: err.code, message: err.message, traceId: err.traceId })
      throw err
    }
    return resp.json()
  },
  // services（治理面 scope=admin 取全集；默认按用户可见集合过滤）
  listServices: (scope) => req('GET', `/api/v1/services${scope === 'admin' ? '?scope=admin' : ''}`),
  getService: (id) => req('GET', `/api/v1/services/${id}`),
  getServiceConfiguration: (id) => req('GET', `/api/v1/services/${id}/configuration`),
  registerService: (b) => req('POST', '/api/v1/services', b),
  refreshService: (id) => req('POST', `/api/v1/services/${id}:refresh`),
  probeService: (id) => req('POST', `/api/v1/services/${id}:probe`),
  probeServiceUrl: (agentCardUrl) => req('POST', '/api/v1/services:probe', { agentCardUrl }),
  enableService: (id) => req('POST', `/api/v1/services/${id}:enable`),
  disableService: (id) => req('POST', `/api/v1/services/${id}:disable`),
  updateService: (id, b) => req('PATCH', `/api/v1/services/${id}`, b),
  deleteService: (id, confirm, force = false) => req(
    'DELETE',
    `/api/v1/services/${id}?confirm=${encodeURIComponent(confirm)}${force ? '&force=true' : ''}`,
  ),
  // 服务级出站凭证（headers=null 清除；值后端 AES-GCM 加密落库，不回显）
  updateServiceCredentials: (id, headers) => req('PUT', `/api/v1/services/${id}/credentials`, { headers }),
  // agents
  listAgents: () => req('GET', '/api/v1/agents'),
  createMaster: (b) => req('POST', '/api/v1/agents', b),
  // HIAGENT 注册：同一统一入口，body 带 sourceType（hiagent-integration/01）。
  // 注册即连通性测试（失败 502 不落库），成功立即挂载 /a2a/{slug}/
  createHiagentAgent: (b) => req('POST', '/api/v1/agents', b),
  // 级联删除，不可恢复：后端要求 confirm 等于该 Team 的 slug
  deleteMaster: (id, confirm) => req(
    'DELETE', `/api/v1/agents/${id}?confirm=${encodeURIComponent(confirm)}`),
  getMaster: (id) => req('GET', `/api/v1/agents/${id}`),
  updateReactConfig: (id, body) => req('PUT', `/api/v1/agents/${id}/react-config`, body),
  // 模型配置（master-model-config 专题）：Agent Team 独立模型（脱钩单一模型）
  updateModelConfig: (id, b) => req('PUT', `/api/v1/agents/${id}/model-config`, b),
  clearModelConfig: (id) => req('DELETE', `/api/v1/agents/${id}/model-config`),
  putBindings: (id, bindings) => req('PUT', `/api/v1/agents/${id}/bindings`, { bindings }),
  listPrompts: (id, withText) => req('GET', `/api/v1/agents/${id}/prompts${withText ? '?with_text=true' : ''}`),
  createPrompt: (id, promptText) => req('POST', `/api/v1/agents/${id}/prompts`, { promptText, source: 'MANUAL' }),
  publishPrompt: (id, vid) => req('POST', `/api/v1/agents/${id}/prompts/${vid}:publish`),
  // opts: { limit, offset, source, onlyProblems }
  // source: all | a2a（业务方经 A2A 端点调入）| testrun | perf
  masterRuns: (id, opts = {}) => {
    const qs = new URLSearchParams()
    if (opts.limit) qs.set('limit', opts.limit)
    if (opts.offset) qs.set('offset', opts.offset)
    if (opts.source && opts.source !== 'all') qs.set('source', opts.source)
    if (opts.onlyProblems) qs.set('only_problems', 'true')
    const suffix = qs.toString() ? `?${qs}` : ''
    return req('GET', `/api/v1/agents/${id}/runs${suffix}`)
  },
  // 试运行：管理台内直接发起一次真实调用，返回回答 + 路由轨迹 + 工具箱快照。
  // body: { text, contextId?, asUser?, inboundFraming? }
  testRunMaster: (id, body) => req('POST', `/api/v1/agents/${id}:test-run`, body),
  // 删除运行记录。sel 必须二选一：{ contextId } 删一段对话，
  // { contextPrefix } 删该前缀下全部（后端拒绝空选择器，防误清）。
  deleteMasterRuns: (id, sel = {}) => {
    const qs = new URLSearchParams()
    if (sel.contextId) qs.set('context_id', sel.contextId)
    if (sel.contextPrefix) qs.set('context_prefix', sel.contextPrefix)
    return req('DELETE', `/api/v1/agents/${id}/runs?${qs}`)
  },
  // permissions
  listGroups: () => req('GET', '/api/v1/groups'),
  createGroup: (name) => req('POST', '/api/v1/groups', { name }),
  deleteGroup: (gid) => req('DELETE', `/api/v1/groups/${gid}`),
  putGroupMembers: (gid, names) => req('PUT', `/api/v1/groups/${gid}/members`, { capUserNames: names }),
  putGroupServices: (gid, ids) => req('PUT', `/api/v1/groups/${gid}/services`, { serviceIds: ids }),
  // PUT 是全量替换语义：保存前必须先用 getUserServices 回填，否则会静默清空旧授权
  getUserServices: (user) => req('GET', `/api/v1/users/${encodeURIComponent(user)}/services`),
  putUserServices: (user, ids) => req('PUT', `/api/v1/users/${encodeURIComponent(user)}/services`, { serviceIds: ids }),
  listUserDirects: () => req('GET', '/api/v1/users/directs'),
  // 系统见过的工号候选池（非员工名录，见后端 docstring）
  listKnownUsers: () => req('GET', '/api/v1/users/known'),
  myVisibility: () => req('GET', '/api/v1/users/me/visibility'),
  // 权限控制 v1：用户管理（admin）与我的权限
  listAdminUsers: () => req('GET', '/api/v1/admin/users'),
  getAdminUser: (userId) => req('GET', `/api/v1/admin/users/${encodeURIComponent(userId)}`),
  searchUserDirectory: (body) => req('POST', '/api/v1/admin/user-directory/search', body),
  getUserDirectory: (userId) => req('GET', `/api/v1/admin/user-directory/${encodeURIComponent(userId)}`),
  createAdminUser: (userId) => req('POST', '/api/v1/admin/users', { userId }),
  getLocalLogin: () => req('GET', '/api/v1/admin/auth/local-login'),
  setLocalLogin: (enabled) => req('PUT', '/api/v1/admin/auth/local-login', { enabled }),
  setUserRole: (userId, isAdmin) =>
    req('PUT', `/api/v1/admin/users/${encodeURIComponent(userId)}/role`, { isAdmin }),
  pagePermissionCatalog: () => req('GET', '/api/v1/admin/page-permissions/catalog'),
  getUserPagePermissions: (userId) => req('GET', `/api/v1/admin/users/${encodeURIComponent(userId)}/page-permissions`),
  putUserPagePermissions: (userId, permissions) => req('PUT', `/api/v1/admin/users/${encodeURIComponent(userId)}/page-permissions`, { permissions }),
  getGroupPagePermissions: (groupId) => req('GET', `/api/v1/admin/groups/${encodeURIComponent(groupId)}/page-permissions`),
  putGroupPagePermissions: (groupId, permissions) => req('PUT', `/api/v1/admin/groups/${encodeURIComponent(groupId)}/page-permissions`, { permissions }),
  myPagePermissions: () => req('GET', '/api/v1/me/page-permissions'),
  setUserStatus: (userId, status) =>
    req('PUT', `/api/v1/admin/users/${encodeURIComponent(userId)}/status`, { status }),
  myPermissions: () => req('GET', '/api/v1/me/permissions'),
  serviceSelector: (forPage) => req('GET', `/api/v1/selectors/services?forPage=${encodeURIComponent(forPage)}`),
  agentSelector: (forPage) => req('GET', `/api/v1/selectors/agents?forPage=${encodeURIComponent(forPage)}`),
  releaseSelector: (forPage, serviceId) => req('GET', `/api/v1/selectors/releases?forPage=${encodeURIComponent(forPage)}&serviceId=${encodeURIComponent(serviceId)}`),
  kbSelector: (forPage) => req('GET', `/api/v1/selectors/knowledge-bases?forPage=${encodeURIComponent(forPage)}`),
  agentLoopContext: (id) => req('GET', `/api/v1/selectors/agents/${encodeURIComponent(id)}/loop-context`),
  userPermissionDetail: (userId) => req('GET', `/api/v1/admin/users/${encodeURIComponent(userId)}/permissions`),
  // eval / optimizer
  listRuns: (params = '') => req('GET', `/api/v1/runs${params}`),
  createDataset: (b) => req('POST', '/api/v1/datasets', b),
  listDatasets: (masterId) => req('GET', `/api/v1/datasets${masterId ? `?master_id=${masterId}` : ''}`),
  renameDataset: (id, name) => req('PATCH', `/api/v1/datasets/${id}`, { name }),
  deleteDataset: (id) => req('DELETE', `/api/v1/datasets/${id}`),
  // 运行监控/调用链上的「加入测试集」入口
  appendCases: (id, runIds) => req('POST', `/api/v1/datasets/${id}/cases`, { runIds }),
  listCases: (id) => req('GET', `/api/v1/datasets/${id}/cases`),
  annotate: (caseId, b) => req('POST', `/api/v1/cases/${caseId}/annotations`, b),
  createOptimization: (b) => req('POST', '/api/v1/optimizations', b),
  // 任务列表：优化跑几分钟，页面不能靠内存里的 id 记住它
  listOptimizations: (masterId) => req('GET', `/api/v1/optimizations${masterId ? `?master_id=${masterId}` : ''}`),
  getOptimization: (id) => req('GET', `/api/v1/optimizations/${id}`),
  // L5 候选人工复核（approve=发布 DRAFT；reject=驳回）
  approveCandidate: (oid, cid) => req('POST', `/api/v1/optimizations/${oid}/candidates/${cid}/approve`),
  rejectCandidate: (oid, cid) => req('POST', `/api/v1/optimizations/${oid}/candidates/${cid}/reject`),
  // releases / installations（P0-2 治理）
  listReleases: (serviceId) => req('GET', `/api/v1/services/${serviceId}/releases`),
  createRelease: (serviceId, b) => req('POST', `/api/v1/services/${serviceId}/releases`, b ?? {}),
  publishRelease: (id) => req('POST', `/api/v1/releases/${id}:publish`),
  suspendRelease: (id) => req('POST', `/api/v1/releases/${id}:suspend`),
  revokeRelease: (id) => req('POST', `/api/v1/releases/${id}:revoke`),
  listInstallations: (ws) => req('GET', `/api/v1/workspaces/${ws}/installations`),
  installRelease: (ws, b) => req('POST', `/api/v1/workspaces/${ws}/installations`, b),
  patchInstallation: (id, status) => req('PATCH', `/api/v1/installations/${id}`, { status }),
  uninstallInstallation: (id) => req('DELETE', `/api/v1/installations/${id}`),
  // instances（P1-2 实例发现）
  listInstances: (serviceId) => req('GET', `/api/v1/services/${serviceId}/instances`),
  registerInstance: (b) => req('POST', '/api/v1/instances/register', b),
  heartbeatInstance: (id, ttl) => req('POST', `/api/v1/instances/${id}:heartbeat${ttl ? `?lease_ttl_seconds=${ttl}` : ''}`),
  deregisterInstance: (id) => req('POST', `/api/v1/instances/${id}:deregister`),
  // traces（P0-4 调用血缘）
  getTrace: (traceId) => req('GET', `/api/v1/traces/${traceId}`),
  // 知识库（from_llm_wiki 02 §5.1；上传成功 plan_pending=true → 轮询 DRAFT 规划）
  listKbs: () => req('GET', '/api/v1/knowledge-bases'),
  createKb: (b) => req('POST', '/api/v1/knowledge-bases', b),
  getKb: (id) => req('GET', `/api/v1/knowledge-bases/${id}`),
  updateKb: (id, b) => req('PUT', `/api/v1/knowledge-bases/${id}`, b),
  deleteKb: (id, force) => req('DELETE', `/api/v1/knowledge-bases/${id}${force ? '?force=true' : ''}`),
  uploadKbSource: (id, file) => api.upload(`/api/v1/knowledge-bases/${id}/sources`, file),
  listKbSources: (id) => req('GET', `/api/v1/knowledge-bases/${id}/sources`),
  deleteKbSource: (id, sid) => req('DELETE', `/api/v1/knowledge-bases/${id}/sources/${sid}`),
  replanKbSource: (id, sid) => req('POST', `/api/v1/knowledge-bases/${id}/sources/${sid}:plan`),
  kbIngest: (id) => req('POST', `/api/v1/knowledge-bases/${id}:ingest`),
  kbRebuild: (id) => req('POST', `/api/v1/knowledge-bases/${id}:rebuild`),
  kbEmbed: (id) => req('POST', `/api/v1/knowledge-bases/${id}:embed`),
  listKbTasks: (id) => req('GET', `/api/v1/knowledge-bases/${id}/tasks`),
  getKbTask: (id, tid) => req('GET', `/api/v1/knowledge-bases/${id}/tasks/${tid}`),
  cancelKbTask: (id, tid) => req('POST', `/api/v1/knowledge-bases/${id}/tasks/${tid}:cancel`),
  listKbPlans: (id, status) => req('GET', `/api/v1/knowledge-bases/${id}/ingest-plans${status ? `?status=${status}` : ''}`),
  getKbPlan: (id, pid) => req('GET', `/api/v1/knowledge-bases/${id}/ingest-plans/${pid}`),
  confirmKbPlan: (id, pid, b) => req('POST', `/api/v1/knowledge-bases/${id}/ingest-plans/${pid}:confirm`, b),
  rejectKbPlan: (id, pid, reason) => req('POST', `/api/v1/knowledge-bases/${id}/ingest-plans/${pid}:reject`, { reason }),
  listKbReviews: (id, status) => req('GET', `/api/v1/knowledge-bases/${id}/reviews${status ? `?status=${status}` : ''}`),
  resolveKbReview: (id, rid, status) => req('POST', `/api/v1/knowledge-bases/${id}/reviews/${rid}:resolve`, { status }),
  listKbPages: (id) => req('GET', `/api/v1/knowledge-bases/${id}/pages`),
  listKbRevs: (id) => req('GET', `/api/v1/knowledge-bases/${id}/revs`),
  kbPreviewSearch: (id, q, topK) => req('GET', `/api/v1/knowledge-bases/${id}/preview/search?q=${encodeURIComponent(q)}${topK ? `&top_k=${topK}` : ''}`),
  putKbBindings: (masterId, bindings) => req('POST', `/api/v1/agents/${masterId}/kb-bindings`, { bindings }),
  getKbBindings: (masterId) => req('GET', `/api/v1/agents/${masterId}/kb-bindings`),
  // 性能测试（perf-test 01 §6）：数据集 / 测试任务
  listPerfDatasets: (masterId) => req('GET', `/api/v1/perf/datasets${masterId ? `?masterId=${masterId}` : ''}`),
  createPerfDataset: (b) => req('POST', '/api/v1/perf/datasets', b),
  uploadPerfDataset: (id, file) => api.upload(`/api/v1/perf/datasets/${id}:upload`, file),
  getPerfDataset: (id, page = 1) => req('GET', `/api/v1/perf/datasets/${id}?page=${page}`),
  deletePerfDataset: (id) => req('DELETE', `/api/v1/perf/datasets/${id}`),
  listPerfTests: (masterId) => req('GET', `/api/v1/perf/tests${masterId ? `?masterId=${masterId}` : ''}`),
  createPerfTest: (b) => req('POST', '/api/v1/perf/tests', b),
  getPerfTest: (id) => req('GET', `/api/v1/perf/tests/${id}`),
  listPerfSamples: (id, status, page = 1) => req('GET', `/api/v1/perf/tests/${id}/samples?page=${page}${status ? `&status=${status}` : ''}`),
  cancelPerfTest: (id) => req('POST', `/api/v1/perf/tests/${id}:cancel`),
  // 网关配置（higress-mcp-gateway 01 §5.2）：profile / MCP 服务 / 路由 / consumer / 审计
  listGatewayProfiles: () => req('GET', '/api/v1/gateway/profiles'),
  createGatewayProfile: (b) => req('POST', '/api/v1/gateway/profiles', b),
  updateGatewayProfile: (id, b) => req('PUT', `/api/v1/gateway/profiles/${id}`, b),
  deleteGatewayProfile: (id) => req('DELETE', `/api/v1/gateway/profiles/${id}`),
  gatewayProfileStatus: (id) => req('GET', `/api/v1/gateway/profiles/${id}/status`),
  listGatewayServices: (pid) => req('GET', `/api/v1/gateway/profiles/${pid}/mcp-services`),
  registerGatewayService: (pid, b) => req('POST', `/api/v1/gateway/profiles/${pid}/mcp-services`, b),
  deleteGatewayService: (pid, name) => req('DELETE', `/api/v1/gateway/profiles/${pid}/mcp-services/${encodeURIComponent(name)}`),
  listGatewayRoutes: (pid) => req('GET', `/api/v1/gateway/profiles/${pid}/routes`),
  createGatewayRoute: (pid, b) => req('POST', `/api/v1/gateway/profiles/${pid}/routes`, b),
  updateGatewayRoute: (pid, name, b) => req('PUT', `/api/v1/gateway/profiles/${pid}/routes/${encodeURIComponent(name)}`, b),
  deleteGatewayRoute: (pid, name) => req('DELETE', `/api/v1/gateway/profiles/${pid}/routes/${encodeURIComponent(name)}`),
  listGatewayConsumers: (pid) => req('GET', `/api/v1/gateway/profiles/${pid}/consumers`),
  createGatewayConsumer: (pid, b) => req('POST', `/api/v1/gateway/profiles/${pid}/consumers`, b),
  rotateGatewayConsumer: (pid, name) => req('POST', `/api/v1/gateway/profiles/${pid}/consumers/${encodeURIComponent(name)}/rotate`),
  deleteGatewayConsumer: (pid, name) => req('DELETE', `/api/v1/gateway/profiles/${pid}/consumers/${encodeURIComponent(name)}`),
  listGatewayAudits: (pid, params = '') => req('GET', `/api/v1/gateway/profiles/${pid}/audits${params}`),
  exportGatewayConfig: (pid, secrets) => req('GET', `/api/v1/gateway/profiles/${pid}/export${secrets ? '?include_secrets=true' : ''}`),
  importGatewayConfig: (pid, payload) => req('POST', `/api/v1/gateway/profiles/${pid}/import`, payload),

  // ===== 开发计划 P0+P1（feature/dev-plan-m1 后端配套）=====
  // DEV-10 流量切换
  listTrafficPolicies: () => req('GET', '/api/v1/traffic-policies'),
  getTrafficPolicy: (serviceId) => req('GET', `/api/v1/services/${serviceId}/traffic-policy`),
  putTrafficPolicy: (serviceId, entries) => req('PUT', `/api/v1/services/${serviceId}/traffic-policy`, { entries }),
  delTrafficPolicy: (serviceId) => req('DELETE', `/api/v1/services/${serviceId}/traffic-policy`),
  // DEV-11 灰度
  createCanary: (serviceId, b) => req('POST', `/api/v1/services/${serviceId}/canary`, b),
  getCanary: (serviceId) => req('GET', `/api/v1/services/${serviceId}/canary`),
  evalCanary: (serviceId) => req('POST', `/api/v1/services/${serviceId}/canary:evaluate`),
  promoteCanary: (serviceId) => req('POST', `/api/v1/services/${serviceId}/canary:promote`),
  abortCanary: (serviceId) => req('POST', `/api/v1/services/${serviceId}/canary:abort`),
  // DEV-12 监控告警 / 远程日志
  listAlerts: (serviceId) => req('GET', `/api/v1/observability/alerts${serviceId ? `?serviceId=${serviceId}` : ''}`),
  listRemoteLogs: (params = '') => req('GET', `/api/v1/observability/remote-logs${params}`),
  // DEV-01 分层记忆（user 只能查/写本人，后端以 cap-user-name 校验）
  listMemory: (scope, ownerId) => req('GET', `/api/v1/memory?scope=${scope}&ownerId=${encodeURIComponent(ownerId)}`),
  addMemory: (b) => req('POST', '/api/v1/memory', b),
  clearMemory: (scope, ownerId) => req('DELETE', `/api/v1/memory?scope=${scope}&ownerId=${encodeURIComponent(ownerId)}`),
  // DEV-17 人效工具
  generateFaqs: (masterId) => req('POST', `/api/v1/efficiency/faqs:generate${masterId ? `?masterId=${masterId}` : ''}`),
  listFaqs: (status) => req('GET', `/api/v1/efficiency/faqs${status ? `?status=${status}` : ''}`),
  decideFaq: (faqId, b) => req('POST', `/api/v1/efficiency/faqs/${faqId}:decide`, b),
  scanGaps: (masterId) => req('POST', `/api/v1/efficiency/gaps:scan${masterId ? `?masterId=${masterId}` : ''}`),
  listGaps: (status) => req('GET', `/api/v1/efficiency/gaps${status ? `?status=${status}` : ''}`),
  resolveGap: (gapId) => req('POST', `/api/v1/efficiency/gaps/${gapId}:resolve`),
  // DEV-05 外部工具
  listExternalTools: () => req('GET', '/api/v1/external-tools'),
  createExternalTool: (b) => req('POST', '/api/v1/external-tools', b),
  updateExternalTool: (id, b) => req('PUT', `/api/v1/external-tools/${id}`, b),
  delExternalTool: (id) => req('DELETE', `/api/v1/external-tools/${id}`),
  enableExternalTool: (id) => req('POST', `/api/v1/external-tools/${id}:enable`),
  disableExternalTool: (id) => req('POST', `/api/v1/external-tools/${id}:disable`),
  // DEV-09 LLM 测试分析报告
  analyzePerfTest: (testId) => req('POST', `/api/v1/perf/tests/${testId}:analyze`),
  getPerfAnalysis: (testId) => req('GET', `/api/v1/perf/tests/${testId}/analysis`),
}
