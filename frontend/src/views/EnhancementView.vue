<script setup>
// 平台增强（开发计划 P0+P1 前端配套）：
// 流量切换/灰度（DEV-10/11）、监控告警（DEV-12）、分层记忆（DEV-01）、
// 人效工具（DEV-17）、外部工具（DEV-05）五个面板；DEV-04 SSE 属调用方
// 传输能力（无管理面）、DEV-07 文本指标在调优页、DEV-09 在性能页。
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { api } from '../api.js'
import { notify, success } from '../notify.js'
import { confirmAction } from '../ui.js'

import Pagination from '../components/Pagination.vue'
import { useServerPagination } from '../pagination.js'

defineProps({ canManage: { type: Boolean, default: false } })

const tabs = [
  { id: 'traffic', label: '流量与灰度' },
  { id: 'alerts', label: '监控告警' },
  { id: 'memory', label: '分层记忆' },
  { id: 'efficiency', label: '人效工具' },
  { id: 'external', label: '外部工具' },
]
const tab = ref('traffic')
function ok(msg) { success(msg) }

// ============ 流量与灰度 ============
const services = ref([])
const svcId = ref('')
const releases = ref([])
const policy = ref(null)
const entries = ref([])          // [{releaseId, weight}]
const canary = ref(null)

async function loadServices() {
  const out = await api.serviceSelector('enhance')
  services.value = out.items.map(s => ({ service_id: s.serviceId, slug: s.slug, display_name: s.displayName }))
}
async function pickService(id) {
  svcId.value = id
  await Promise.all([loadReleases(), loadPolicy(), loadCanary()])
}
async function loadReleases() {
  const out = await api.releaseSelector('enhance', svcId.value)
  releases.value = (out.items || []).map(r => ({ release_id: r.releaseId, agent_version: r.agentVersion, state: r.state })).filter(r => r.state === 'PUBLISHED')
}
async function loadPolicy() {
  policy.value = await api.getTrafficPolicy(svcId.value)
  entries.value = policy.value.entries
    ? policy.value.entries.map(e => ({ releaseId: e.release_id ?? e.releaseId, weight: e.weight }))
    : []
}
async function savePolicy() {
  const total = entries.value.reduce((s, e) => s + (Number(e.weight) || 0), 0)
  if (total !== 100) { notify({ code: 'VALIDATION', message: `权重合计须为 100，当前 ${total}` }); return }
  await api.putTrafficPolicy(svcId.value, entries.value)
  ok('流量策略已生效（调用即刻按比例分流）')
  await loadPolicy()
}
function addEntry() { entries.value.push({ releaseId: '', weight: 0 }) }
async function removePolicy() {
  if (!await confirmAction('删除流量策略并回切默认解析链？', { danger: true })) return
  await api.delTrafficPolicy(svcId.value)
  ok('已回切默认解析链')
  await loadPolicy()
}
// —— 灰度 ——
const canaryForm = ref({ candidate: '', ratio: 0.1, whitelist: '' })
async function loadCanary() {
  canary.value = await api.getCanary(svcId.value)
  if (canary.value.status === 'RUNNING') canaryForm.value.candidate = canary.value.candidate_release_id
}
async function startCanary() {
  if (!canaryForm.value.candidate) { notify({ code: 'VALIDATION', message: '请选择候选版本' }); return }
  await api.createCanary(svcId.value, {
    candidateReleaseId: canaryForm.value.candidate,
    ratio: Number(canaryForm.value.ratio) || 0.1,
    whitelist: canaryForm.value.whitelist.split(/[,，\s]+/).filter(Boolean),
  })
  ok('灰度已开启（白名单恒走候选版，其余按比例稳定哈希分组）')
  await loadCanary()
}
async function doEvaluate() {
  const r = await api.evalCanary(svcId.value)
  ok(`守护判定：${r.action}${r.reason ? ' — ' + r.reason : ''}`)
  await loadCanary()
}
async function doPromote() {
  if (!await confirmAction('候选版全量（比例改 100%）？')) return
  await api.promoteCanary(svcId.value)
  ok('已全量到候选版本（保持观察态）')
  await loadCanary()
}
async function doAbort() {
  if (!await confirmAction('紧急停止灰度并恢复默认解析链？', { danger: true })) return
  await api.abortCanary(svcId.value)
  ok('灰度已停止')
  await loadCanary()
}
function fmtMs(v) { return v == null ? '—' : v + 'ms' }

// ============ 监控告警 ============
const alerts = ref([])
const alertsLoading = ref(false)
const alertsPager = useServerPagination(alerts, () => '/api/v1/observability/alerts')
async function loadAlerts() {
  alertsLoading.value = true
  try { await alertsPager.load() } finally { alertsLoading.value = false }
}
let alertsTimer = null
onMounted(() => {
  loadServices()   // 流量/灰度面板的服务下拉（默认 Tab，进页面即需可用）
  loadAlerts()
  alertsTimer = setInterval(loadAlerts, 15000)
})
onUnmounted(() => clearInterval(alertsTimer))
// 切到各面板时自动带出数据：不再依赖用户先点操作按钮才有内容
watch(tab, (t) => {
  if (t === 'traffic' && !services.value.length) loadServices()
  if (t === 'efficiency') { loadFaqs(); loadGaps(); loadKbs() }
  if (t === 'external') loadExt()
})

// ============ 分层记忆 ============
const memScope = ref('user')
const memOwner = ref(api.user)
const memories = ref([])
const memContent = ref('')
const memoriesPager = useServerPagination(memories, () => `/api/v1/memory?scope=${memScope.value}&ownerId=${encodeURIComponent(memOwner.value)}`)
async function loadMemory() { await memoriesPager.load() }
async function addMemoryItem() {
  if (!memContent.value.trim()) return
  await api.addMemory({ scope: memScope.value, ownerId: memOwner.value, content: memContent.value })
  memContent.value = ''
  ok('记忆已写入（Team 下次运行即注入提示词）')
  await loadMemory()
}
async function clearMemoryItems() {
  if (!await confirmAction(`清除 ${memScope.value} · ${memOwner.value} 的全部记忆？`, { danger: true })) return
  const r = await api.clearMemory(memScope.value, memOwner.value)
  ok(`已清除 ${r.deleted} 条`)
  await loadMemory()
}

// ============ 人效工具 ============
const faqStatus = ref('PENDING')
const faqs = ref([])
const gaps = ref([])
const kbOptions = ref([])
const faqsPager = useServerPagination(faqs, () => `/api/v1/efficiency/faqs?status=${faqStatus.value}`)
async function loadFaqs() { await faqsPager.load() }
const gapsPager = useServerPagination(gaps, () => '/api/v1/efficiency/gaps')
async function loadGaps() { await gapsPager.load() }
async function loadKbs() {
  const out = await api.kbSelector('enhance')
  kbOptions.value = out.items.map(k => ({ kb_id: k.kbId, display_name: k.displayName, slug: k.slug }))
}
async function doGenerate() {
  const r = await api.generateFaqs()
  ok(`FAQ 挖掘完成：聚类 ${r.clusters} 组，新建 ${r.created} 条候选`)
  faqStatus.value = 'PENDING'
  await loadFaqs()
}
async function doScanGaps() {
  const r = await api.scanGaps()
  ok(`缺口扫描完成：聚类 ${r.clusters} 组`)
  await loadGaps()
}
async function approveFaq(f, kbId) {
  await api.decideFaq(f.faq_id, { approve: true, kbId: kbId || null })
  ok(kbId ? '已通过并自动进入 KB 入库规划' : '已通过（未选知识库，仅标记 APPROVED）')
  await loadFaqs()
}
async function rejectFaq(f) {
  await api.decideFaq(f.faq_id, { approve: false })
  await loadFaqs()
}
async function resolveGapItem(g) {
  await api.resolveGap(g.gap_id)
  ok('缺口已关闭')
  await loadGaps()
}

// ============ 外部工具 ============
const extTools = ref([])
const extForm = ref({ kind: 'kb_query', name: '', description: '', endpoint: '', headers: '' })
const extEditing = ref(null)     // 编辑中的 tool_id；null = 注册模式
const extHeadersPlaceholder = computed(() => extEditing.value
  ? '请求头 JSON（留空 = 保持原凭证，加密不回显）'
  : '请求头 JSON（可空），如 {“Authorization”: “Bearer xx”} — 加密落库永不回显')
const extFormDefault = { kind: 'kb_query', name: '', description: '', endpoint: '', headers: '' }
const extPager = useServerPagination(extTools, () => '/api/v1/external-tools')
async function loadExt() { await extPager.load() }
function editExt(t) {
  extEditing.value = t.tool_id
  extForm.value = { kind: t.kind, name: t.name, description: t.description,
                    endpoint: t.endpoint, headers: '' }  // 凭证不回填：留空=保持
}
function cancelExtEdit() {
  extEditing.value = null
  extForm.value = { ...extFormDefault }
}
async function submitExt() {
  const f = extForm.value
  if (!f.name || !f.endpoint || !f.description) { notify({ code: 'VALIDATION', message: '名称/描述/endpoint 必填' }); return }
  let headers
  const hasHeaders = f.headers.trim().length > 0
  if (hasHeaders) { try { headers = JSON.parse(f.headers) } catch { notify({ code: 'VALIDATION', message: 'headers 须是 JSON 对象' }); return } }
  if (extEditing.value) {
    const body = { description: f.description, endpoint: f.endpoint }
    if (hasHeaders) body.headers = headers   // 缺省 = 保持原凭证
    await api.updateExternalTool(extEditing.value, body)
    cancelExtEdit()
    ok('外部工具已更新')
  } else {
    await api.createExternalTool({ kind: f.kind, name: f.name, description: f.description, endpoint: f.endpoint, ...(hasHeaders ? { headers } : {}) })
    extForm.value = { ...extFormDefault }
    ok('外部工具已注册（加密凭证落库，Team 工具箱即时可用）')
  }
  await loadExt()
}
async function toggleExt(t) { await (t.enabled ? api.disableExternalTool(t.tool_id) : api.enableExternalTool(t.tool_id)); await loadExt() }
async function delExt(t) {
  if (!await confirmAction(`删除外部工具 ${t.name}？`, { danger: true })) return
  await api.delExternalTool(t.tool_id)
  await loadExt()
}

const svcName = (id) => services.value.find(s => s.service_id === id)?.display_name || id
const relVer = (id) => releases.value.find(r => r.release_id === id)?.agent_version || id
</script>

<template>
  <div>
    <div class="flex items-center justify-between mb-5">
      <div>
        <h2 class="text-xl font-semibold">平台增强</h2>
        <p class="text-t3 text-sm mt-1">流量切换 · 灰度发布 · 监控告警 · 分层记忆 · 人效工具 · 外部服务</p>
      </div>
    </div>
    <div class="flex gap-2 mb-5 border-b border-line">
      <button v-for="t in tabs" :key="t.id" :class="['px-4 h-9 text-sm font-medium rounded-t-lg border-b-2 -mb-px',
        tab === t.id ? 'border-brand text-brand' : 'border-transparent text-t3 hover:text-t1']" @click="tab = t.id">
        {{ t.label }}
      </button>
    </div>

    <!-- 流量与灰度 -->
    <div v-if="tab === 'traffic'">
      <div class="flex items-center gap-3 mb-4">
        <span class="text-sm text-t2">服务</span>
        <select class="input !w-72" @change="pickService($event.target.value)">
          <option value="" disabled :selected="!svcId">选择服务…</option>
          <option v-for="s in services" :key="s.service_id" :value="s.service_id" :selected="s.service_id === svcId">{{ s.display_name }}（{{ s.slug }}）</option>
        </select>
      </div>
      <template v-if="svcId">
        <div class="grid grid-cols-2 gap-5">
          <div class="border border-line rounded-xl p-4">
            <div class="flex items-center justify-between mb-3">
              <h3 class="font-semibold">流量切换</h3>
              <span v-if="policy?.status === 'ACTIVE'" class="badge badge-ok">生效中</span>
              <span v-else class="badge badge-gray">默认链</span>
            </div>
            <p class="text-xs text-t3 mb-3">PUBLISHED 版本按权重分流（合计 100）；选中版本不可用自动让位，全挂回默认链。显式 pin 的绑定不受影响。</p>
            <div v-for="(e, i) in entries" :key="i" class="flex items-center gap-2 mb-2">
              <select v-model="e.releaseId" class="input !w-auto flex-1">
                <option value="" disabled>选择版本…</option>
                <option v-for="r in releases" :key="r.release_id" :value="r.release_id">v{{ r.agent_version }}（{{ r.release_id.slice(0, 12) }}）</option>
              </select>
              <input v-model.number="e.weight" type="number" min="1" max="100" class="input !w-20" placeholder="权重">
              <span class="text-xs text-t3">%</span>
              <button v-if="canManage" class="btn-text text-sm" @click="entries.splice(i, 1)">移除</button>
            </div>
            <div class="flex gap-2 mt-3">
              <button v-if="canManage" class="btn btn-ghost btn-sm" @click="addEntry">+ 加版本</button>
              <button v-if="canManage" class="btn btn-primary btn-sm" @click="savePolicy">保存策略</button>
              <button v-if="canManage && policy?.status === 'ACTIVE'" class="btn btn-ghost btn-sm" @click="removePolicy">一键回切</button>
            </div>
          </div>

          <div class="border border-line rounded-xl p-4">
            <div class="flex items-center justify-between mb-3">
              <h3 class="font-semibold">灰度测试</h3>
              <span v-if="canary?.status === 'RUNNING'" class="badge badge-warn">灰度中</span>
              <span v-else class="badge badge-gray">未开启</span>
            </div>
            <p class="text-xs text-t3 mb-3">与流量策略互斥。白名单用户恒走候选版，其余按比例稳定哈希分组；候选错误率超限自动熔断。</p>
            <template v-if="canary?.status === 'RUNNING'">
              <div class="bg-canvas rounded-lg p-3 text-sm mb-3">
                <div class="flex justify-between py-1"><span>候选版本</span><b>v{{ relVer(canary.candidate_release_id) }}</b></div>
                <div class="flex justify-between py-1"><span>基线版本</span><b>v{{ relVer(canary.baseline_release_id) }}</b></div>
                <div class="flex justify-between py-1"><span>分流比例</span><b>{{ (canary.group.ratio * 100).toFixed(0) }}%</b></div>
                <div class="flex justify-between py-1"><span>白名单</span><b>{{ canary.group.whitelist?.length ? canary.group.whitelist.join(', ') : '—' }}</b></div>
              </div>
              <div class="grid grid-cols-2 gap-3 mb-3">
                <div class="border border-line rounded-lg p-3 text-xs">
                  <div class="font-semibold mb-1">候选组</div>
                  <div>样本 {{ canary.metrics?.candidate?.samples ?? 0 }} ｜ 错误率 {{ canary.metrics?.candidate?.error_rate ?? '—' }}</div>
                  <div>平均 {{ fmtMs(canary.metrics?.candidate?.avg_ms) }}</div>
                </div>
                <div class="border border-line rounded-lg p-3 text-xs">
                  <div class="font-semibold mb-1">基线组</div>
                  <div>样本 {{ canary.metrics?.baseline?.samples ?? 0 }} ｜ 错误率 {{ canary.metrics?.baseline?.error_rate ?? '—' }}</div>
                  <div>平均 {{ fmtMs(canary.metrics?.baseline?.avg_ms) }}</div>
                </div>
              </div>
              <div class="flex gap-2">
                <button v-if="canManage" class="btn btn-ghost btn-sm" @click="doEvaluate">守护判定</button>
                <button v-if="canManage" class="btn btn-primary btn-sm" @click="doPromote">达标全量</button>
                <button v-if="canManage" class="btn btn-ghost btn-sm !text-brand" @click="doAbort">紧急停止</button>
              </div>
            </template>
            <template v-else>
              <div class="space-y-2">
                <select v-model="canaryForm.candidate" class="input">
                  <option value="" disabled>候选版本（默认版本之外）…</option>
                  <option v-for="r in releases" :key="r.release_id" :value="r.release_id">v{{ r.agent_version }}（{{ r.release_id.slice(0, 12) }}）</option>
                </select>
                <div class="flex gap-2">
                  <input v-model.number="canaryForm.ratio" type="number" min="0.01" max="1" step="0.05" class="input" placeholder="分流比例 0.1">
                  <input v-model="canaryForm.whitelist" class="input" placeholder="白名单工号，逗号分隔（可空）">
                </div>
                <button v-if="canManage" class="btn btn-primary btn-sm" @click="startCanary">开启灰度</button>
              </div>
            </template>
          </div>
        </div>
      </template>
    </div>

    <!-- 监控告警 -->
    <div v-if="tab === 'alerts'">
      <div class="flex items-center justify-between mb-3">
        <h3 class="font-semibold">告警事件</h3>
        <div class="flex items-center gap-2">
          <span class="text-xs text-t3">15s 自动刷新</span>
          <button class="btn btn-ghost btn-sm" @click="loadAlerts" :disabled="alertsLoading">刷新</button>
        </div>
      </div>
      <p class="text-xs text-t3 mb-3">定时探测连续不可达触发 SERVICE_DOWN（静默窗防重发），恢复自动关闭并记 SERVICE_RECOVERED。</p>
      <div class="border border-line rounded-xl overflow-hidden">
        <table class="w-full text-sm">
          <thead class="bg-canvas text-t3 text-xs">
            <tr><th class="text-left px-4 py-2.5">时间</th><th class="text-left px-4 py-2.5">服务</th><th class="text-left px-4 py-2.5">类型</th><th class="text-left px-4 py-2.5">状态</th><th class="text-left px-4 py-2.5">详情</th></tr>
          </thead>
          <tbody>
            <tr v-for="a in alerts" :key="a.alert_id" class="border-t border-line">
              <td class="px-4 py-2.5 text-xs text-t3">{{ String(a.created_at).replace('T', ' ').slice(0, 19) }}</td>
              <td class="px-4 py-2.5">{{ svcName(a.service_id) }}</td>
              <td class="px-4 py-2.5"><span :class="a.kind === 'SERVICE_DOWN' ? 'badge badge-err' : 'badge badge-ok'">{{ a.kind }}</span></td>
              <td class="px-4 py-2.5"><span :class="a.status === 'FIRED' ? 'badge badge-warn' : 'badge badge-gray'">{{ a.status }}</span></td>
              <td class="px-4 py-2.5 text-xs text-t2 max-w-[22rem] truncate" :title="a.detail">{{ a.detail }}</td>
            </tr>
            <tr v-if="!alerts.length"><td colspan="5" class="px-4 py-8 text-center text-t3">{{ alertsLoading ? '加载中…' : '暂无告警（服务都健康）' }}</td></tr>
          </tbody>
        </table>
        <Pagination :pager="alertsPager" />
      </div>
    </div>

    <!-- 分层记忆 -->
    <div v-if="tab === 'memory'">
      <h3 class="font-semibold mb-1">分层记忆</h3>
      <p class="text-xs text-t3 mb-4">user=跨会话偏好（注入提示词，本人可管理）；agent=Team 经验沉淀；shared=多 Agent 共享区。user scope 只能查/写本人。</p>
      <div class="flex flex-wrap items-center gap-2 mb-4">
        <select v-model="memScope" class="input !w-32" @change="memOwner = memScope === 'user' ? api.user : memOwner">
          <option value="user">user</option><option value="agent">agent</option><option value="shared">shared</option>
        </select>
        <input v-model="memOwner" class="input !w-72" :placeholder="memScope === 'user' ? '工号' : memScope === 'agent' ? 'master_id' : '共享区名'">
        <button class="btn btn-ghost btn-sm" @click="loadMemory">查询</button>
        <button v-if="canManage" class="btn btn-ghost btn-sm !text-brand" @click="clearMemoryItems">清空该域</button>
      </div>
      <div class="flex gap-2 mb-4">
        <input v-model="memContent" class="input" placeholder="写入一条记忆，如：回复偏好简短列表（脱敏后落库）" @keyup.enter="addMemoryItem">
        <button v-if="canManage" class="btn btn-primary btn-sm shrink-0" @click="addMemoryItem">写入</button>
      </div>
      <div class="border border-line rounded-xl divide-y divide-line">
        <div v-for="m in memories" :key="m.memory_id" class="px-4 py-2.5 text-sm flex justify-between gap-4">
          <span>{{ m.content }}</span>
          <span class="text-xs text-t3 shrink-0">{{ String(m.created_at).replace('T', ' ').slice(0, 19) }}</span>
        </div>
        <div v-if="!memories.length" class="px-4 py-8 text-center text-t3 text-sm">该域暂无记忆</div>
      </div>
      <Pagination :pager="memoriesPager" />
    </div>

    <!-- 人效工具 -->
    <div v-if="tab === 'efficiency'">
      <div class="flex items-center justify-between mb-3">
        <h3 class="font-semibold">FAQ 候选（挖掘自真实运行）</h3>
        <div class="flex items-center gap-2">
          <select v-model="faqStatus" class="input !w-36" @change="loadFaqs">
            <option>PENDING</option><option>APPROVED</option><option>REJECTED</option><option>INGESTED</option>
          </select>
          <button v-if="canManage" class="btn btn-ghost btn-sm" @click="doGenerate">挖掘 FAQ</button>
        </div>
      </div>
      <div class="border border-line rounded-xl divide-y divide-line mb-6">
        <div v-for="f in faqs" :key="f.faq_id" class="px-4 py-3">
          <div class="flex items-center gap-2 mb-1">
            <span class="badge badge-brand">问过 {{ f.freq }} 次</span>
            <span class="badge" :class="f.status === 'PENDING' ? 'badge-warn' : f.status === 'INGESTED' ? 'badge-ok' : 'badge-gray'">{{ f.status }}</span>
          </div>
          <div class="text-sm font-medium">{{ f.question }}</div>
          <div class="text-xs text-t3 mt-1 line-clamp-2">{{ f.answer }}</div>
          <div v-if="f.status === 'PENDING'" class="flex items-center gap-2 mt-2">
            <select class="input flex-1 max-w-72 !h-8 !py-1 text-xs leading-5" @change="approveFaq(f, $event.target.value); $event.target.value = ''">
              <option value="">✅ 通过并入库到…</option>
              <option v-for="k in kbOptions" :key="k.kb_id" :value="k.kb_id">{{ k.display_name }}</option>
            </select>
            <button v-if="canManage" class="btn btn-ghost btn-sm" @click="approveFaq(f, '')">仅通过</button>
            <button v-if="canManage" class="btn-text text-sm !text-t3" @click="rejectFaq(f)">驳回</button>
          </div>
        </div>
        <div v-if="!faqs.length" class="px-4 py-8 text-center text-t3 text-sm">暂无候选——先在 Team 里跑几轮 KB 问答，再点「挖掘 FAQ」</div>
      </div>
      <Pagination :pager="faqsPager" />

      <div class="flex items-center justify-between mb-3">
        <h3 class="font-semibold">知识缺口（KB 拒答聚类）</h3>
        <button v-if="canManage" class="btn btn-ghost btn-sm" @click="doScanGaps">扫描缺口</button>
      </div>
      <div class="border border-line rounded-xl divide-y divide-line">
        <div v-for="g in gaps" :key="g.gap_id" class="px-4 py-3 flex items-center justify-between gap-4">
          <div>
            <div class="text-sm font-medium truncate" :title="g.query_text">{{ g.query_text }}</div>
            <div class="text-xs text-t3 mt-0.5">拒答 {{ g.hit_count }} 次 ｜ 最近 {{ String(g.last_seen_at).replace('T', ' ').slice(0, 16) }}</div>
          </div>
          <button v-if="canManage && g.status === 'OPEN'" class="btn btn-ghost btn-sm shrink-0" @click="resolveGapItem(g)">知识已补齐，关闭</button>
          <span v-else class="badge badge-gray shrink-0">已解决</span>
        </div>
        <div v-if="!gaps.length" class="px-4 py-8 text-center text-t3 text-sm">暂无缺口记录</div>
      </div>
      <Pagination :pager="gapsPager" />
    </div>

    <!-- 外部工具 -->
    <div v-if="tab === 'external'">
      <h3 class="font-semibold mb-1">外部服务工具</h3>
      <p class="text-xs text-t3 mb-4">外部 KB 问答 / 通用 REST 包装为 Team 只读工具（SSRF 守卫出站，凭证加密落库）。HIA 智能体请到「A2A 注册」页以 HiAgent 来源登记（hiagent-integration 专题）。</p>
      <div class="grid grid-cols-2 gap-4 mb-5">
        <select v-model="extForm.kind" class="input" :disabled="!!extEditing">
          <option value="kb_query">kb_query（外部知识库问答）</option>
          <option value="rest">rest（通用 REST 查询）</option>
          <option value="hiagent">HiAgent 对接（预留，暂不可注册）</option>
        </select>
        <input v-model="extForm.name" class="input" placeholder="工具名（英文，如 ext_wiki_kb）" :disabled="!!extEditing">
        <input v-model="extForm.description" class="input" placeholder="描述（会进 Team 工具清单）">
        <input v-model="extForm.endpoint" class="input" placeholder="https://api.example.com/qa">
        <input v-model="extForm.headers" class="input col-span-2" :placeholder="extHeadersPlaceholder">
        <div class="col-span-2 flex gap-2">
          <button v-if="canManage" class="btn btn-primary btn-sm" @click="submitExt">{{ extEditing ? '保存修改' : '注册工具' }}</button>
          <button v-if="extEditing" class="btn btn-ghost btn-sm" @click="cancelExtEdit">取消编辑</button>
        </div>
      </div>
      <div class="border border-line rounded-xl divide-y divide-line">
        <div v-for="t in extTools" :key="t.tool_id" class="px-4 py-3 flex items-center justify-between gap-4">
          <div class="min-w-0">
            <div class="text-sm font-medium">{{ t.name }} <span class="badge badge-gray ml-1">{{ t.kind }}</span></div>
            <div class="text-xs text-t3 truncate mt-0.5">{{ t.description }} → {{ t.endpoint }}</div>
          </div>
          <div class="flex gap-1 shrink-0">
            <button v-if="canManage" class="btn btn-ghost btn-sm" @click="editExt(t)">编辑</button>
            <button v-if="canManage" class="btn btn-ghost btn-sm" @click="toggleExt(t)">{{ t.enabled ? '停用' : '启用' }}</button>
            <button v-if="canManage" class="btn-text text-sm !text-t3" @click="delExt(t)">删除</button>
          </div>
        </div>
        <div v-if="!extTools.length" class="px-4 py-8 text-center text-t3 text-sm">暂无外部工具</div>
      </div>
      <Pagination :pager="extPager" />
    </div>
  </div>
</template>
