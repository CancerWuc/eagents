<script setup>
// 网关配置 Tab（higress-mcp-gateway 03 T17/T18）：
// profile 切换 + 四子页（状态总览 / MCP 服务 / 路由 / consumer）。
// 写操作二次确认；K8s 失败经全局通知展示（GATEWAY_* 错误码映射中文提示）；
// 状态卡片支持 5s 自动轮询。密钥仅在创建/轮换响应中一次性展示。
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from '../api.js'
import { fmtDateTime } from '../format.js'
import { promptText, confirmAction } from '../ui.js'
import { notify } from '../notify.js'

import Pagination from '../components/Pagination.vue'
import { useServerPagination } from '../pagination.js'

defineProps({ canManage: { type: Boolean, default: false } })

const tab = ref('overview')
const profiles = ref([])
const pid = ref(null)
const overview = ref(null)
const services = ref([])
const routes = ref([])
const consumers = ref([])
const audits = ref({ audits: [], total: 0, page: 1 })
const loading = ref(false)
const autoPoll = ref(false)
let pollTimer = null

// ---- 表单状态 ----
const showProfileForm = ref(false)
const profileForm = ref({ name: '', higressNs: '', kubeContext: '' })
const showSvcForm = ref(false)
const svcForm = ref({
  name: '', domain: '', port: 443, protocol: 'https', registryType: 'dns',
  withProvider: false, provider: { type: 'deepseek', protocol: 'openai', apiUrl: '', apiTokens: '' },
  withRoute: true, route: {
    matchType: 'PRE', matchValue: '', weight: 100, authEnabled: true,
    retryEnabled: true, retryAttempts: 3, consumerName: '',
  },
})
const showRouteForm = ref(false)
const routeForm = ref({
  name: '', matchType: 'PRE', matchValue: '', upstreamName: '', weight: 100,
  authEnabled: true, retryEnabled: true, retryAttempts: 3, consumers: '',
})
const showConsumerForm = ref(false)
const consumerForm = ref({ name: '', routes: '' })
const revealedKey = ref(null) // 一次性明文密钥展示 {title, credential}
const auditFilter = ref('')

const serviceOptions = ref([])
const consumerOptions = ref([])
const auditRows = ref([])
const gatewayUrl = suffix => `/api/v1/gateway/profiles/${pid.value}/${suffix}`
const servicesPager = useServerPagination(services, () => gatewayUrl('mcp-services'))
const routesPager = useServerPagination(routes, () => gatewayUrl('routes'))
const consumersPager = useServerPagination(consumers, () => gatewayUrl('consumers'))
const auditsPager = useServerPagination(auditRows, () => gatewayUrl('audits') + `?resourceType=${auditFilter.value}`, {
  onResult: result => { audits.value = { ...result, audits: result.items } },
})
const profile = computed(() => profiles.value.find(p => p.id === pid.value) || null)
const serviceNames = computed(() => serviceOptions.value.map(s => s.name))
const consumerNames = computed(() => consumerOptions.value.map(c => c.name))

function friendly(err) {
  // T18: GATEWAY_* 错误码 → 中文操作提示（原 message 保留在通知里）
  const map = {
    GATEWAY_K8S_FORBIDDEN: 'K8s 权限不足：请检查 RBAC（deploy/rbac-gateway.yaml）与 ServiceAccount',
    GATEWAY_RESOURCE_NOT_FOUND: '网关资源不存在（可能已被 console 或他人删除）',
    GATEWAY_CONFLICT: '写入冲突：资源被并发修改（console 共存期），请刷新后重试',
    GATEWAY_K8S_ERROR: 'K8s API 调用失败：检查集群连通性 / kube context',
    GATEWAY_CONFIG_MISSING: '未配置 EAGENT_GATEWAY_MASTER_KEY（base64 32B），无法管理密钥',
  }
  return map[err?.code] || err?.message || '操作失败'
}

function compBadge(s) {
  return { ready: 'badge badge-ok', not_ready: 'badge badge-err', absent: 'badge badge-gray', unknown: 'badge badge-gray' }[s] || 'badge badge-gray'
}
function resultBadge(r) { return r === 'success' ? 'badge badge-ok' : 'badge badge-err' }

async function loadProfiles(keepPid = true) {
  const out = await api.listGatewayProfiles()
  profiles.value = out.profiles
  if (!profiles.value.length) {
    pid.value = null
    return
  }
  if (!keepPid || !profiles.value.find(p => p.id === pid.value)) {
    pid.value = profiles.value[0].id
  }
}

async function loadAll() {
  if (!pid.value) return
  const id = pid.value
  loading.value = true
  try {
    const [st] = await Promise.all([
      api.gatewayProfileStatus(pid.value),
      servicesPager.load(), routesPager.load(), consumersPager.load(),
    ])
    if (pid.value !== id) return
    overview.value = st
    const [sv, cs] = await Promise.all([api.listGatewayServices(id), api.listGatewayConsumers(id)])
    if (pid.value !== id) return
    serviceOptions.value = sv.services
    consumerOptions.value = cs.consumers
    await loadAudits()
  } catch (e) {
    notify({ code: e.code, message: friendly(e), traceId: e.traceId })
  } finally { loading.value = false }
}

async function loadAudits() {
  if (!pid.value) return
  await auditsPager.load()
}

async function onProfileChange() { overview.value = null; await loadAll() }

// ---- profile 管理 ----
async function createProfile() {
  const f = profileForm.value
  if (!f.name || !f.higressNs) return
  try {
    await api.createGatewayProfile({ name: f.name, higressNs: f.higressNs, kubeContext: f.kubeContext })
    showProfileForm.value = false
    profileForm.value = { name: '', higressNs: '', kubeContext: '' }
    await loadProfiles(false)
    await loadAll()
  } catch (e) { notify({ code: e.code, message: friendly(e), traceId: e.traceId }) }
}
async function removeProfile() {
  const p = profile.value
  if (!p) return
  if (!await confirmAction(`删除网关环境「${p.name}」？仍有受管资源时会拒绝。`, { okText: '删除', danger: true })) return
  try {
    await api.deleteGatewayProfile(p.id)
    await loadProfiles(false)
    await loadAll()
  } catch (e) { notify({ code: e.code, message: friendly(e), traceId: e.traceId }) }
}

// ---- MCP 服务（一键四步注册）----
function resetSvcForm() {
  svcForm.value = {
    name: '', domain: '', port: 443, protocol: 'https', registryType: 'dns',
    withProvider: false,
    provider: { type: 'deepseek', protocol: 'openai', apiUrl: '', apiTokens: '' },
    withRoute: true,
    route: {
      matchType: 'PRE', matchValue: '', weight: 100, authEnabled: true,
      retryEnabled: true, retryAttempts: 3, consumerName: '',
    },
  }
}
async function registerService() {
  const f = svcForm.value
  if (!f.name || !f.domain) return
  const body = {
    name: f.name, domain: f.domain, port: Number(f.port) || 443,
    protocol: f.protocol, registryType: f.registryType,
  }
  if (f.withProvider) {
    body.provider = {
      type: f.provider.type, protocol: f.provider.protocol,
      apiUrl: f.provider.apiUrl,
      apiTokens: f.provider.apiTokens ? f.provider.apiTokens.split(',').map(s => s.trim()).filter(Boolean) : [],
    }
  }
  if (f.withRoute) {
    body.route = {
      matchType: f.route.matchType, matchValue: f.route.matchValue || '/',
      weight: Number(f.route.weight) || 100, authEnabled: f.route.authEnabled,
      retryEnabled: f.route.retryEnabled,
      retryAttempts: Number(f.route.retryAttempts) || 3,
      consumerName: f.route.consumerName || undefined,
    }
  }
  try {
    const out = await api.registerGatewayService(pid.value, body)
    showSvcForm.value = false
    resetSvcForm()
    if (out.consumer?.credential) {
      revealedKey.value = {
        title: `consumer「${out.consumer.name}」密钥（仅此一次展示，请立即保存）`,
        credential: out.consumer.credential,
      }
    }
    await loadAll()
  } catch (e) { notify({ code: e.code, message: friendly(e), traceId: e.traceId }) }
}
async function removeService(s) {
  if (!await confirmAction(`移除 MCP 服务「${s.name}」？将同时删除 registry${s.provider_config ? ' 与 ai-proxy provider' : ''}；有路由引用时会拒绝。`, { okText: '移除', danger: true })) return
  try {
    await api.deleteGatewayService(pid.value, s.name)
    await loadAll()
  } catch (e) { notify({ code: e.code, message: friendly(e), traceId: e.traceId }) }
}

// ---- 路由 ----
async function createRoute() {
  const f = routeForm.value
  if (!f.name || !f.matchValue || !f.upstreamName) return
  const cs = f.consumers ? f.consumers.split(',').map(s => s.trim()).filter(Boolean) : []
  for (const c of cs) {
    if (!consumerNames.value.includes(c)) {
      notify({ code: 'VALIDATION_ERROR', message: `consumer「${c}」不存在，请先创建` })
      return
    }
  }
  try {
    await api.createGatewayRoute(pid.value, {
      name: f.name, matchType: f.matchType, matchValue: f.matchValue,
      upstreamName: f.upstreamName, weight: Number(f.weight) || 100,
      authEnabled: f.authEnabled, retryEnabled: f.retryEnabled,
      retryAttempts: Number(f.retryAttempts) || 3, consumers: cs,
    })
    showRouteForm.value = false
    routeForm.value = { name: '', matchType: 'PRE', matchValue: '', upstreamName: '', weight: 100, authEnabled: true, retryEnabled: true, retryAttempts: 3, consumers: '' }
    await loadAll()
  } catch (e) { notify({ code: e.code, message: friendly(e), traceId: e.traceId }) }
}
async function toggleRouteAuth(r) {
  try {
    await api.updateGatewayRoute(pid.value, r.name, { authEnabled: !r.auth_enabled })
    await loadAll()
  } catch (e) { notify({ code: e.code, message: friendly(e), traceId: e.traceId }) }
}
async function editRouteConsumers(r) {
  const raw = await promptText(
    `路由「${r.name}」绑定的 consumer（逗号分隔，当前: ${r.consumers.join(',') || '无'}）`,
    r.consumers.join(','),
  )
  if (raw === null) return
  const cs = raw.split(',').map(s => s.trim()).filter(Boolean)
  for (const c of cs) {
    if (!consumerNames.value.includes(c)) {
      notify({ code: 'VALIDATION_ERROR', message: `consumer「${c}」不存在` })
      return
    }
  }
  try {
    await api.updateGatewayRoute(pid.value, r.name, { consumers: cs })
    await loadAll()
  } catch (e) { notify({ code: e.code, message: friendly(e), traceId: e.traceId }) }
}
async function removeRoute(r) {
  if (!await confirmAction(`删除路由「${r.name}」？将删除 ConfigMap ${r.cm_name} 并解绑 consumer。`, { okText: '删除', danger: true })) return
  try {
    await api.deleteGatewayRoute(pid.value, r.name)
    await loadAll()
  } catch (e) { notify({ code: e.code, message: friendly(e), traceId: e.traceId }) }
}

// ---- consumer ----
async function createConsumer() {
  const f = consumerForm.value
  if (!f.name) return
  const rs = f.routes ? f.routes.split(',').map(s => s.trim()).filter(Boolean) : []
  try {
    const out = await api.createGatewayConsumer(pid.value, { name: f.name, routes: rs })
    showConsumerForm.value = false
    consumerForm.value = { name: '', routes: '' }
    revealedKey.value = {
      title: `consumer「${out.name}」密钥（仅此一次展示，请立即保存）`,
      credential: out.credential,
    }
    await loadAll()
  } catch (e) { notify({ code: e.code, message: friendly(e), traceId: e.traceId }) }
}
async function rotateConsumer(c) {
  if (!await confirmAction(`轮换「${c.name}」的密钥？宽限期内新旧密钥同时可用，到期旧密钥自动失效。`, { okText: '轮换' })) return
  try {
    const out = await api.rotateGatewayConsumer(pid.value, c.name)
    revealedKey.value = {
      title: `consumer「${c.name}」新密钥（仅此一次展示，宽限期 ${out.graceSeconds}s 内旧密钥仍可用）`,
      credential: out.credential,
    }
    await loadAll()
  } catch (e) { notify({ code: e.code, message: friendly(e), traceId: e.traceId }) }
}
async function removeConsumer(c) {
  if (!await confirmAction(`删除 consumer「${c.name}」？将同步从绑定路由的 allowedConsumers 中摘除。`, { okText: '删除', danger: true })) return
  try {
    await api.deleteGatewayConsumer(pid.value, c.name)
    await loadAll()
  } catch (e) { notify({ code: e.code, message: friendly(e), traceId: e.traceId }) }
}

// ---- 导出 / 导入 ----
async function doExport(withSecrets) {
  try {
    const out = await api.exportGatewayConfig(pid.value, withSecrets)
    const blob = new Blob([JSON.stringify(out, null, 2)], { type: 'application/json' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `gateway-config-${profile.value?.name || pid.value}.json`
    a.click()
    URL.revokeObjectURL(a.href)
  } catch (e) { notify({ code: e.code, message: friendly(e), traceId: e.traceId }) }
}
async function doImport() {
  if (!await confirmAction('导入会按四步写序列在当前环境创建资源，consumer 密钥会重新生成。确认继续？', { okText: '导入' })) return
  const input = document.createElement('input')
  input.type = 'file'
  input.accept = '.json'
  input.onchange = async () => {
    const file = input.files?.[0]
    if (!file) return
    try {
      const payload = JSON.parse(await file.text())
      const out = await api.importGatewayConfig(pid.value, payload)
      const failed = (out.failed || []).length
      notify({
        code: failed ? 'IMPORT_PARTIAL' : 'IMPORT_OK',
        message: failed
          ? `导入完成：成功 ${out.created.length} 项，失败 ${failed} 项（${out.failed.map(f => `${f.service || f.consumer}: ${f.error}`).join('；')}）`
          : `导入完成：${out.created.length} 项资源已创建`,
      })
      await loadAll()
    } catch (e) { notify({ code: e.code, message: friendly(e), traceId: e.traceId }) }
  }
  input.click()
}

// ---- 5s 状态轮询（T18 可选项）----
function setAutoPoll(on) {
  autoPoll.value = on
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
  if (on) pollTimer = setInterval(async () => {
    if (!pid.value) return
    try { overview.value = await api.gatewayProfileStatus(pid.value) } catch { /* 静默：轮询失败不打扰 */ }
  }, 5000)
}

onMounted(async () => {
  await loadProfiles(false)
  await loadAll()
})
onBeforeUnmount(() => setAutoPoll(false))
</script>

<template>
  <div>
    <!-- 头部：profile 切换 + 操作 -->
    <div class="flex items-center justify-between mb-5 flex-wrap gap-3">
      <div>
        <h2 class="text-xl font-semibold">网关配置（Higress）</h2>
        <p class="text-sm text-t3 mt-1">在这里配置 Higress 网关：注册 MCP 服务、配置 AI 路由、管理访问密钥。
        <span class="text-t3">eAgent 只负责下发配置，实际流量不经过 eAgent，仍然直连 Higress。
        下方 consumer / registry / route 等均为 Higress 原生概念，与 Higress 官方文档一致。</span></p>
      </div>
      <div class="flex items-center gap-2 flex-wrap">
        <select v-if="profiles.length" v-model="pid" class="input !w-48" @change="onProfileChange">
          <option v-for="p in profiles" :key="p.id" :value="p.id">{{ p.name }}（{{ p.higress_ns }}）</option>
        </select>
        <button v-if="canManage" class="btn btn-ghost" @click="showProfileForm = true">+ 环境</button>
        <button v-if="canManage && profile" class="btn btn-ghost" @click="doExport(false)">导出</button>
        <button v-if="canManage && profile" class="btn btn-ghost" @click="doExport(true)">导出（含密钥）</button>
        <button v-if="canManage && profile" class="btn btn-ghost" @click="doImport">导入</button>
        <button v-if="canManage && profile" class="btn btn-ghost" @click="removeProfile">删除环境</button>
      </div>
    </div>

    <div v-if="!profiles.length" class="card text-center text-t3 py-12">
      暂无网关环境。点击「+ 环境」创建（如 aliyun-dev / higress-system），或部署时配置
      EAGENT_GATEWAY_PROFILE_NAME / EAGENT_GATEWAY_HIGRESS_NS 自动种子。
    </div>

    <template v-else>
      <!-- 子页切换 -->
      <div class="flex gap-1 border-b border-line mb-5">
        <button v-for="t in [['overview', '状态总览'], ['services', 'MCP 服务'], ['routes', '路由'], ['consumers', 'Consumer']]"
          :key="t[0]"
          :class="['px-4 h-10 text-sm font-medium border-b-2 -mb-px cursor-pointer', tab === t[0] ? 'border-brand text-brand' : 'border-transparent text-t2 hover:text-t1']"
          @click="tab = t[0]">{{ t[1] }}</button>
      </div>

      <!-- ============ 状态总览 ============ -->
      <div v-if="tab === 'overview'">
        <div class="card">
          <div class="flex items-center justify-between mb-4">
            <h3 class="font-semibold">连通性探测</h3>
            <label class="flex items-center gap-2 text-sm text-t2 cursor-pointer">
              <input type="checkbox" :checked="autoPoll" @change="setAutoPoll($event.target.checked)"> 5s 自动刷新
            </label>
          </div>
          <div v-if="overview" class="grid grid-cols-4 gap-3">
            <div v-for="(s, comp) in { controller: overview.connectivity.controller, gateway: overview.connectivity.gateway, console: overview.connectivity.console }"
                 :key="comp" class="border border-line rounded-lg p-3">
              <div class="text-xs text-t3 mb-1">{{ comp }}</div>
              <span :class="compBadge(s)">{{ s }}</span>
            </div>
            <div class="border border-line rounded-lg p-3">
              <div class="text-xs text-t3 mb-1">Higress 版本</div>
              <span class="badge badge-brand">{{ overview.connectivity.higressVersion || '未知' }}</span>
            </div>
          </div>
          <div v-if="overview" class="grid grid-cols-5 gap-3 mt-3">
            <div class="border border-line rounded-lg p-3 text-center">
              <div class="text-xs text-t3">MCP 服务</div>
              <div class="text-xl font-semibold mt-1">{{ overview.counts.mcpServices }}</div>
            </div>
            <div class="border border-line rounded-lg p-3 text-center">
              <div class="text-xs text-t3">线上 registry</div>
              <div class="text-xl font-semibold mt-1">{{ overview.counts.liveRegistries ?? '—' }}</div>
            </div>
            <div class="border border-line rounded-lg p-3 text-center">
              <div class="text-xs text-t3">路由</div>
              <div class="text-xl font-semibold mt-1">{{ overview.counts.routes }}</div>
            </div>
            <div class="border border-line rounded-lg p-3 text-center">
              <div class="text-xs text-t3">Consumer</div>
              <div class="text-xl font-semibold mt-1">{{ overview.counts.consumers }}</div>
            </div>
            <div class="border border-line rounded-lg p-3 text-center">
              <div class="text-xs text-t3">最近写入</div>
              <div class="text-xs font-medium mt-2">{{ fmtDateTime(overview.latestWriteAt, '') }}</div>
            </div>
          </div>
          <div v-if="overview && !overview.connectivity.connected" class="mt-3 text-sm text-red-600">
            集群不可达：{{ overview.connectivity.error }}（检查 kube context / RBAC / EAGENT_KUBECONFIG）
          </div>
        </div>

        <div class="card !p-0">
          <div class="flex items-center justify-between px-6 py-4">
            <h3 class="font-semibold">审计日志（写操作全量落库）</h3>
            <select v-model="auditFilter" class="input !w-44" @change="loadAudits">
              <option value="">全部类型</option>
              <option v-for="t in ['profile', 'mcpbridge', 'wasmplugin', 'route', 'consumer', 'sync']" :key="t" :value="t">{{ t }}</option>
            </select>
          </div>
          <div class="overflow-x-auto">
            <table class="tbl">
              <thead><tr><th>时间</th><th>资源</th><th>动作</th><th>操作人</th><th>结果</th><th>详情</th></tr></thead>
              <tbody>
                <tr v-if="!audits.audits.length"><td colspan="6" class="text-center text-t3 py-8">暂无审计记录</td></tr>
                <tr v-for="a in audits.audits" :key="a.id">
                  <td class="text-xs">{{ fmtDateTime(a.created_at, '') }}</td>
                  <td><span class="badge badge-gray">{{ a.resource_type }}</span></td>
                  <td>{{ a.action }}</td>
                  <td>{{ a.operator }}</td>
                  <td><span :class="resultBadge(a.result)">{{ a.result }}</span></td>
                  <td class="text-xs text-t3 max-w-[420px] truncate" :title="JSON.stringify(a.payload_after)">{{ JSON.stringify(a.payload_after) }}</td>
                </tr>
              </tbody>
            </table>
          <Pagination :pager="auditsPager" />
          </div>
        </div>
      </div>

      <!-- ============ MCP 服务 ============ -->
      <div v-if="tab === 'services'" class="card !p-0">
        <div class="flex items-center justify-between px-6 py-4">
          <h3 class="font-semibold">MCP 服务（McpBridge registry）</h3>
          <button v-if="canManage" class="btn btn-primary" @click="showSvcForm = !showSvcForm; resetSvcForm()">+ 注册 MCP 服务</button>
        </div>

        <div v-if="showSvcForm" class="px-6 pb-4 border-b border-line">
          <div class="grid grid-cols-2 gap-3">
            <div><label class="text-xs text-t3">服务名 *</label><input v-model="svcForm.name" class="input" placeholder="如 my-llm-1（registry = llm-llm-my-llm-1.internal）" /></div>
            <div><label class="text-xs text-t3">上游域名 *</label><input v-model="svcForm.domain" class="input" placeholder="如 api.deepseek.com" /></div>
            <div><label class="text-xs text-t3">端口</label><input v-model="svcForm.port" class="input" type="number" /></div>
            <div><label class="text-xs text-t3">协议</label>
              <select v-model="svcForm.protocol" class="input"><option>https</option><option>http</option></select></div>
            <div><label class="text-xs text-t3">registry 类型</label>
              <select v-model="svcForm.registryType" class="input"><option>dns</option><option>static</option><option>failover</option></select></div>
          </div>
          <label class="flex items-center gap-2 text-sm mt-3 cursor-pointer">
            <input type="checkbox" v-model="svcForm.withProvider"> 配置 ai-proxy provider（四步写序列第 2 步）
          </label>
          <div v-if="svcForm.withProvider" class="grid grid-cols-2 gap-3 mt-2">
            <div><label class="text-xs text-t3">provider 类型</label><input v-model="svcForm.provider.type" class="input" placeholder="deepseek / openai / qwen ..." /></div>
            <div><label class="text-xs text-t3">协议</label><input v-model="svcForm.provider.protocol" class="input" placeholder="openai" /></div>
            <div><label class="text-xs text-t3">apiUrl</label><input v-model="svcForm.provider.apiUrl" class="input" placeholder="https://api.deepseek.com/v1" /></div>
            <div><label class="text-xs text-t3">apiTokens（逗号分隔）</label><input v-model="svcForm.provider.apiTokens" class="input" placeholder="sk-..." /></div>
          </div>
          <label class="flex items-center gap-2 text-sm mt-3 cursor-pointer">
            <input type="checkbox" v-model="svcForm.withRoute"> 同步创建路由 + consumer（一键四步写序列）
          </label>
          <div v-if="svcForm.withRoute" class="grid grid-cols-3 gap-3 mt-2">
            <div><label class="text-xs text-t3">匹配类型</label>
              <select v-model="svcForm.route.matchType" class="input"><option>PRE</option><option>EXACT</option><option>REGEX</option></select></div>
            <div><label class="text-xs text-t3">路径匹配值</label><input v-model="svcForm.route.matchValue" class="input" placeholder="/my-llm-1" /></div>
            <div><label class="text-xs text-t3">consumer 名（缺省自动）</label><input v-model="svcForm.route.consumerName" class="input" placeholder="consumer-my-llm-1" /></div>
          </div>
          <label class="flex items-center gap-2 text-sm mt-2 cursor-pointer">
            <input type="checkbox" v-model="svcForm.route.authEnabled"> 启用 key-auth 鉴权（无 key 请求 401）
          </label>
          <div class="flex items-center justify-end gap-3 mt-4">
            <!-- 四步写序列是「会发生什么」的说明，属于正文而非按钮标签 -->
            <span class="text-xs text-t3 mr-auto font-mono">
              写入顺序：McpBridge → ai-proxy → key-auth → ai-route
            </span>
            <button class="btn btn-ghost" @click="showSvcForm = false">取消</button>
            <button class="btn btn-primary" @click="registerService">注册</button>
          </div>
        </div>

        <div class="overflow-x-auto">
          <table class="tbl">
            <thead><tr><th>名称 / registry</th><th>上游</th><th>provider</th><th>状态</th><th>创建人</th><th>更新时间</th><th></th></tr></thead>
            <tbody>
              <tr v-if="!services.length && !loading"><td colspan="7" class="text-center text-t3 py-10">暂无 MCP 服务</td></tr>
              <tr v-for="s in services" :key="s.id">
                <td>
                  <div class="font-medium">{{ s.name }}</div>
                  <div class="text-xs text-t3 font-mono">{{ s.registry_name }}</div>
                </td>
                <td class="text-sm">{{ s.domain }}:{{ s.port }} · {{ s.protocol }}</td>
                <td>
                  <span v-if="s.provider_config" class="badge badge-brand">{{ s.provider_config.type }}</span>
                  <span v-else class="text-t3 text-xs">—</span>
                </td>
                <td><span class="badge badge-ok">{{ s.status }}</span></td>
                <td class="text-sm">{{ s.created_by }}</td>
                <td class="text-xs">{{ fmtDateTime(s.updated_at, '') }}</td>
                <td><button v-if="canManage" class="btn btn-text text-red-600" @click="removeService(s)">移除</button></td>
              </tr>
            </tbody>
          </table>
          <Pagination :pager="servicesPager" />
        </div>
      </div>

      <!-- ============ 路由 ============ -->
      <div v-if="tab === 'routes'" class="card !p-0">
        <div class="flex items-center justify-between px-6 py-4">
          <h3 class="font-semibold">AI 路由（ai-route ConfigMap）</h3>
          <button v-if="canManage" class="btn btn-primary" @click="showRouteForm = !showRouteForm">+ 创建路由</button>
        </div>

        <div v-if="showRouteForm" class="px-6 pb-4 border-b border-line">
          <div class="grid grid-cols-3 gap-3">
            <div><label class="text-xs text-t3">路由名 *</label><input v-model="routeForm.name" class="input" placeholder="route-my-llm-1" /></div>
            <div><label class="text-xs text-t3">匹配类型</label>
              <select v-model="routeForm.matchType" class="input"><option>PRE</option><option>EXACT</option><option>REGEX</option></select></div>
            <div><label class="text-xs text-t3">路径匹配值 *</label><input v-model="routeForm.matchValue" class="input" placeholder="/my-llm-1" /></div>
            <div><label class="text-xs text-t3">上游 MCP 服务 *</label>
              <select v-model="routeForm.upstreamName" class="input">
                <option value="" disabled>选择 MCP 服务</option>
                <option v-for="n in serviceNames" :key="n" :value="n">{{ n }}</option>
              </select></div>
            <div><label class="text-xs text-t3">权重</label><input v-model="routeForm.weight" class="input" type="number" /></div>
            <div><label class="text-xs text-t3">绑定 consumer（逗号分隔）</label><input v-model="routeForm.consumers" class="input" placeholder="consumer-my-llm-1" /></div>
          </div>
          <label class="flex items-center gap-2 text-sm mt-3 cursor-pointer">
            <input type="checkbox" v-model="routeForm.authEnabled"> 启用 key-auth 鉴权
          </label>
          <div class="flex justify-end gap-2 mt-4">
            <button class="btn btn-ghost" @click="showRouteForm = false">取消</button>
            <button class="btn btn-primary" @click="createRoute">创建</button>
          </div>
        </div>

        <div class="overflow-x-auto">
          <table class="tbl">
            <thead><tr><th>名称 / ConfigMap</th><th>匹配</th><th>上游</th><th>鉴权</th><th>consumer</th><th>重试</th><th></th></tr></thead>
            <tbody>
              <tr v-if="!routes.length && !loading"><td colspan="7" class="text-center text-t3 py-10">暂无路由</td></tr>
              <tr v-for="r in routes" :key="r.id">
                <td>
                  <div class="font-medium">{{ r.name }}</div>
                  <div class="text-xs text-t3 font-mono">{{ r.cm_name }}</div>
                </td>
                <td><span class="badge badge-gray">{{ r.match_type }}</span> <span class="text-sm font-mono">{{ r.match_value }}</span></td>
                <td class="text-sm">{{ r.upstream_name }} · {{ r.weight }}%</td>
                <td>
                  <button v-if="canManage" :class="['badge cursor-pointer', r.auth_enabled ? 'badge-ok' : 'badge-gray']" @click="toggleRouteAuth(r)">
                    {{ r.auth_enabled ? '开启' : '关闭' }}
                  </button>
                </td>
                <td class="text-xs">{{ r.consumers.join(', ') || '—' }}</td>
                <td class="text-xs">{{ r.retry_enabled ? `${r.retry_attempts} 次` : '关' }}</td>
                <td class="whitespace-nowrap">
                  <button v-if="canManage" class="btn btn-text" @click="editRouteConsumers(r)">绑定</button>
                  <button v-if="canManage" class="btn btn-text text-red-600" @click="removeRoute(r)">删除</button>
                </td>
              </tr>
            </tbody>
          </table>
          <Pagination :pager="routesPager" />
        </div>
      </div>

      <!-- ============ Consumer ============ -->
      <div v-if="tab === 'consumers'" class="card !p-0">
        <div class="flex items-center justify-between px-6 py-4">
          <h3 class="font-semibold">Consumer（key-auth 密钥）</h3>
          <button v-if="canManage" class="btn btn-primary" @click="showConsumerForm = !showConsumerForm">+ 创建 consumer</button>
        </div>

        <div v-if="showConsumerForm" class="px-6 pb-4 border-b border-line">
          <div class="grid grid-cols-2 gap-3">
            <div><label class="text-xs text-t3">名称 *</label><input v-model="consumerForm.name" class="input" placeholder="consumer-my-llm-1" /></div>
            <div><label class="text-xs text-t3">绑定路由（逗号分隔，可选）</label><input v-model="consumerForm.routes" class="input" placeholder="route-my-llm-1" /></div>
          </div>
          <p class="text-xs text-t3 mt-2">密钥自动生成（随机 32B base64url），创建后仅展示一次。</p>
          <div class="flex justify-end gap-2 mt-4">
            <button class="btn btn-ghost" @click="showConsumerForm = false">取消</button>
            <button class="btn btn-primary" @click="createConsumer">创建</button>
          </div>
        </div>

        <div class="overflow-x-auto">
          <table class="tbl">
            <thead><tr><th>名称</th><th>密钥</th><th>绑定路由</th><th>轮换状态</th><th>创建人</th><th>更新时间</th><th></th></tr></thead>
            <tbody>
              <tr v-if="!consumers.length && !loading"><td colspan="7" class="text-center text-t3 py-10">暂无 consumer</td></tr>
              <tr v-for="c in consumers" :key="c.id">
                <td class="font-medium">{{ c.name }}</td>
                <td><code class="text-xs bg-canvas px-1.5 py-0.5 rounded">{{ c.credential }}</code></td>
                <td class="text-xs">{{ c.routes.join(', ') || '—' }}</td>
                <td>
                  <span v-if="c.in_grace" class="badge badge-warn">宽限期 → {{ fmtDateTime(c.prev_expires_at, '') }}</span>
                  <span v-else class="badge badge-gray">稳定</span>
                </td>
                <td class="text-sm">{{ c.created_by }}</td>
                <td class="text-xs">{{ fmtDateTime(c.updated_at, '') }}</td>
                <td class="whitespace-nowrap">
                  <button v-if="canManage" class="btn btn-text" @click="rotateConsumer(c)">轮换</button>
                  <button v-if="canManage" class="btn btn-text text-red-600" @click="removeConsumer(c)">删除</button>
                </td>
              </tr>
            </tbody>
          </table>
          <Pagination :pager="consumersPager" />
        </div>
      </div>
    </template>

    <!-- 一次性密钥展示（创建 / 轮换 / 导入） -->
    <div v-if="revealedKey" class="fixed inset-0 bg-black/30 flex items-center justify-center z-50" @click.self="revealedKey = null">
      <div class="bg-white rounded-xl p-6 w-[560px] shadow-xl">
        <h3 class="font-semibold text-base mb-3">{{ revealedKey.title }}</h3>
        <code class="block bg-canvas rounded-lg p-3 text-sm break-all select-all">{{ revealedKey.credential }}</code>
        <p class="text-xs text-t3 mt-2">请求头用法：Authorization: Bearer {{ revealedKey.credential }}</p>
        <div class="flex justify-end gap-2 mt-4">
          <button class="btn btn-ghost" @click="navigator.clipboard?.writeText(revealedKey.credential)">复制</button>
          <button class="btn btn-primary" @click="revealedKey = null">我已保存</button>
        </div>
      </div>
    </div>

    <!-- 新建环境 -->
    <div v-if="showProfileForm" class="fixed inset-0 bg-black/30 flex items-center justify-center z-50" @click.self="showProfileForm = false">
      <div class="bg-white rounded-xl p-6 w-[440px] shadow-xl">
        <h3 class="font-semibold text-base mb-4">新建网关环境</h3>
        <label class="text-xs text-t3">环境名</label>
        <input v-model="profileForm.name" class="input mb-3" placeholder="aliyun-dev" />
        <label class="text-xs text-t3">Higress 资源 namespace</label>
        <input v-model="profileForm.higressNs" class="input mb-3" placeholder="higress-system" />
        <label class="text-xs text-t3">kube context（集群内运行留空）</label>
        <input v-model="profileForm.kubeContext" class="input" placeholder="" />
        <div class="flex justify-end gap-2 mt-4">
          <button class="btn btn-ghost" @click="showProfileForm = false">取消</button>
          <button class="btn btn-primary" @click="createProfile">创建</button>
        </div>
      </div>
    </div>
  </div>
</template>
