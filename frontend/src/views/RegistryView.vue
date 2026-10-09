<script setup>
import HiAgentRunDialog from '../components/HiAgentRunDialog.vue'
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { api } from '../api.js'
import { config } from '../config.js'
import { buildHiagentRegistration, restoreHiagentRegistration, parameterChoices } from '../hiagent-registration.js'
import { fmtDateTime as fmtTime } from '../format.js'
import { slugValidationError, httpUrlError } from '../form-rules.js'
import { randomUuid } from '../uuid.js'
import Pagination from '../components/Pagination.vue'
import { useServerPagination, watchPagination } from '../pagination.js'

defineProps({ canManage: { type: Boolean, default: false } })

const runService = ref(null)

const services = ref([])
const err = ref('')
const loading = ref(true)
const search = ref('')
const tile = ref('all')          // all | healthy | abnormal | incompat
const enabledFilter = ref('all') // all | on | off
const stats = ref({})
const servicesPager = useServerPagination(services, () => '/api/v1/services?scope=admin', {
  filters: () => ({ q: search.value.trim(), tile: tile.value, enabled: enabledFilter.value }),
  onResult: result => { stats.value = result.stats || {}; closeDrawer() },
})
watchPagination(servicesPager, [search], 300)
watchPagination(servicesPager, [tile, enabledFilter])

// --- 注册弹窗状态 ---
const showForm = ref(false)
const emptyForm = () => ({ slug: '', displayName: '', agentCardUrl: '', tags: '', outboundApiKey: '', description: '', baseUrl: '', appKey: '' })
const form = ref(emptyForm())
const formSource = ref('A2A')    // 注册来源：A2A | HIAGENT
const skills = ref([])           // HIAGENT 生成式 Card 素材 [{name, description}]
const parameterRows = ref([])
const testQuery = ref('你好')
const originalSchema = ref(null)
const parameterConfig = computed(() => buildHiagentRegistration(parameterRows.value, testQuery.value, originalSchema.value))
const busy = ref(false)
const formProbeBusy = ref(false)
const formProbeResult = ref(null)

// --- 行操作状态 ---
const probing = ref('')
const refreshing = ref('')
const togglingId = ref('')
const probingAll = ref(false)
const copied = ref('')

// --- 编辑弹窗状态 ---
const editTarget = ref(null)
const editLoading = ref(false)
const editError = ref('')
const credentialConfigured = ref(false)
const clearCredential = ref(false)
let editRequest = 0

// --- 删除确认弹窗状态 ---
const deleteTarget = ref(null)
const deleteConfirmSlug = ref('')
const deleteForce = ref(false)
const deleteBusy = ref(false)
const deleteBoundMasters = ref([])
const checkingBindings = ref(false)

// --- 卡片详情抽屉状态 ---
const drawerTarget = ref(null)
const drawerLoading = ref(false)
const drawerData = ref(null)

// --- 出站凭证弹窗状态 ---
const credTarget = ref(null)
const credApiKey = ref('')
const credBusy = ref(false)

// --- HIAGENT 来源辅助 ---
const a2aBase = config.apiBase || window.location.origin
const isHiagent = (s) => s.source_type === 'HIAGENT'
// HIAGENT 行的对外地址是平台挂载端点（Card 由平台生成），A2A 行才是远端 well-known
const displayUrl = (s) => isHiagent(s)
  ? `${a2aBase}/a2a/${s.slug}/`
  : `${s.agent_card_url}/.well-known/agent-card.json`

async function load() {
  try {
    await servicesPager.load()
    err.value = ''
  } catch (e) {
    err.value = e.message
  } finally {
    loading.value = false
  }
}
onMounted(load)

// --- 注册表单相关 ---
const slugError = computed(() => {
  const v = form.value.slug.trim()
  if (!v) return ''
  const error = slugValidationError(v)
  if (error) return error
  if (services.value.some(s => s.slug === v && s.service_id !== editTarget.value?.service_id)) return '该标识已被占用'
  return ''
})
const urlError = computed(() => httpUrlError(form.value.agentCardUrl))
const baseUrlError = computed(() => httpUrlError(form.value.baseUrl))
const formValid = computed(() => {
  if (editLoading.value || editError.value) return false
  if (!form.value.slug.trim() || slugError.value) return false
  if (formSource.value === 'HIAGENT') {
    // 后端 MasterIn：HIAGENT 必填 slug/displayName，其余可选（全局默认兜底）
    return !!form.value.displayName.trim() && !baseUrlError.value
      && !parameterConfig.value.queryError && !Object.keys(parameterConfig.value.errors).length
  }
  return !!form.value.agentCardUrl.trim() && !urlError.value
})

function addParameter() {
  parameterRows.value.push({ id: randomUuid(), name: '', type: 'string', required: true, description: '', options: '', testValue: '' })
}
function addSkill() { skills.value.push({ name: '', description: '' }) }
function removeSkill(i) { skills.value.splice(i, 1) }

function openForm() {
  editRequest++
  editTarget.value = null
  editLoading.value = false
  editError.value = ''
  credentialConfigured.value = false
  clearCredential.value = false
  originalSchema.value = null
  form.value = emptyForm()
  formSource.value = 'A2A'
  skills.value = []
  parameterRows.value = []
  testQuery.value = '你好'
  formProbeResult.value = null
  showForm.value = true
}
function closeForm() {
  if (busy.value) return
  showForm.value = false
  editTarget.value = null
  editRequest++
}

// 注册弹窗连接测试
async function probeFormUrl() {
  if (!form.value.agentCardUrl || urlError.value) return
  formProbeBusy.value = true
  formProbeResult.value = null
  try {
    const res = await api.probeServiceUrl(form.value.agentCardUrl.trim())
    formProbeResult.value = res
    if (!form.value.displayName && res?.card?.name) {
      form.value.displayName = res.card.name
    }
  } catch { /* toast 已展示 */ }
  finally { formProbeBusy.value = false }
}

async function submit() {
  if (busy.value || !formValid.value) return
  busy.value = true
  try {
    const editing = !!editTarget.value
    if (formSource.value === 'HIAGENT') {
      // HIAGENT 走统一 Agent 注册入口（POST /api/v1/agents），注册即连通性
      // 测试（建会话 + 发「你好」），失败 502 不落库；成功立即挂载 /a2a/{slug}/
      const payload = {
        sourceType: 'HIAGENT',
        slug: form.value.slug.trim(),
        displayName: form.value.displayName.trim(),
        description: editing ? form.value.description.trim() : form.value.description.trim() || undefined,
        baseUrl: editing ? form.value.baseUrl.trim() : form.value.baseUrl.trim() || undefined,
        appKey: clearCredential.value ? undefined : form.value.appKey.trim() || undefined,
      }
      const sk = skills.value.filter((x) => x.name.trim())
      if (sk.length || editing) {
        payload.skills = sk.map((x) => ({
          ...x, name: x.name.trim(),
          description: x.description.trim() || undefined,
        }))
      }
      payload.inputsSchema = parameterConfig.value.schema
      payload.connectionTest = parameterConfig.value.connectionTest
      if (editing) {
        delete payload.sourceType
        delete payload.slug
        payload.clearCredential = clearCredential.value
        await api.updateService(editTarget.value.service_id, payload)
      } else await api.createHiagentAgent(payload)
    } else {
      // A2A：用户只填 API Key，前端包装成后端已有的 apikey Header 契约。
      const body = {
        slug: form.value.slug,
        displayName: form.value.displayName,
        agentCardUrl: form.value.agentCardUrl,
        tags: form.value.tags,
      }
      const apiKey = (form.value.outboundApiKey || '').trim()
      if (apiKey && !clearCredential.value) body.authSecretRef = { apikey: apiKey }
      if (editing) {
        delete body.slug
        body.clearCredential = clearCredential.value
        await api.updateService(editTarget.value.service_id, body)
      } else await api.registerService(body)
    }
    // 不走 closeForm()：它带 busy 守卫（防提交中点取消），而此刻 busy
    // 仍为 true，会被静默挡掉——注册成功但弹窗不关（TeamView 同场景
    // 直接置 false 是对的）
    showForm.value = false
    editTarget.value = null
    await load()
  } catch { /* toast 已展示 */ }
  finally { busy.value = false }
}

// --- 编辑：按来源回填对应来源的注册表单 ---
async function openEdit(service) {
  openForm()
  editTarget.value = service
  formSource.value = isHiagent(service) ? 'HIAGENT' : 'A2A'
  form.value.slug = service.slug
  await loadEdit()
}
async function loadEdit() {
  const target = editTarget.value
  if (!target) return
  const request = ++editRequest
  editLoading.value = true
  editError.value = ''
  try {
    const cfg = await api.getServiceConfiguration(target.service_id)
    if (request !== editRequest || !showForm.value) return
    formSource.value = cfg.sourceType === 'HIAGENT' ? 'HIAGENT' : 'A2A'
    form.value = { slug: cfg.slug, displayName: cfg.displayName || '', agentCardUrl: cfg.agentCardUrl || '',
      tags: cfg.tags || '', outboundApiKey: '', description: cfg.description || '', baseUrl: cfg.baseUrl || '', appKey: '' }
    credentialConfigured.value = cfg.credentialConfigured
    skills.value = (cfg.skills || []).map(skill => ({ ...skill, name: skill.name || '', description: skill.description || '' }))
    if (formSource.value === 'HIAGENT') {
      const restored = restoreHiagentRegistration(cfg, randomUuid)
      originalSchema.value = restored.schema
      parameterRows.value = restored.rows
      testQuery.value = restored.query
    }
  } catch (e) {
    if (request === editRequest) editError.value = e.message || '读取配置失败，请重试'
  } finally {
    if (request === editRequest) editLoading.value = false
  }
}

// --- 删除服务相关 ---
async function openDelete(s) {
  deleteTarget.value = s
  deleteConfirmSlug.value = ''
  deleteForce.value = false
  deleteBoundMasters.value = []
  checkingBindings.value = true
  try {
    const masters = await api.listAgents()
    const bound = []
    for (const m of (masters || [])) {
      try {
        const detail = await api.getMaster(m.master_id)
        if (detail?.bindings?.some(b => b.service_id === s.service_id)) {
          bound.push(m.slug || m.display_name || m.master_id)
        }
      } catch { /* ignore */ }
    }
    deleteBoundMasters.value = bound
    if (bound.length > 0) {
      deleteForce.value = true
    }
  } catch { /* ignore */ }
  finally {
    checkingBindings.value = false
  }
}
function closeDelete() {
  if (deleteBusy.value) return
  deleteTarget.value = null
}
async function confirmDelete() {
  if (!deleteTarget.value || deleteConfirmSlug.value.trim() !== deleteTarget.value.slug) return
  deleteBusy.value = true
  try {
    await api.deleteService(deleteTarget.value.service_id, deleteConfirmSlug.value.trim(), deleteForce.value)
    deleteTarget.value = null
    await load()
  } catch { /* toast 已展示 */ }
  finally { deleteBusy.value = false }
}

// --- 卡片详情抽屉相关 ---
async function openDrawer(s) {
  drawerTarget.value = s
  drawerLoading.value = true
  drawerData.value = null
  try {
    const result = await api.getService(s.service_id)
    if (drawerTarget.value?.service_id === s.service_id) drawerData.value = result
  } catch { /* toast 已展示 */ }
  finally { drawerLoading.value = false }
}
function closeDrawer() {
  drawerTarget.value = null
}

// --- 出站凭证相关 ---
function openCred(s) {
  credTarget.value = s
  credApiKey.value = ''
}
function closeCred() {
  if (credBusy.value) return
  credTarget.value = null
}
async function saveCred() {
  const value = credApiKey.value.trim()
  // BUG-05 防护：若已配凭证且输入为空，要求二次确认
  if (!value && credTarget.value?.outbound_credential_configured) {
    if (!window.confirm(`确定要清空服务【${credTarget.value.display_name || credTarget.value.slug}】的出站凭证吗？清空后后续调用将不再注入认证 Header！`)) {
      return
    }
  }
  credBusy.value = true
  try {
    await api.updateServiceCredentials(
      credTarget.value.service_id, value ? { apikey: value } : null)
    credTarget.value = null
    await load()
  } catch { /* toast 已展示 */ }
  finally { credBusy.value = false }
}

// --- 刷新与单行检测 ---
async function refresh(s) {
  refreshing.value = s.service_id
  try { await api.refreshService(s.service_id); await load() }
  catch { /* toast 已展示 */ }
  finally { refreshing.value = '' }
}

async function toggle(s) {
  if (togglingId.value) return
  togglingId.value = s.service_id
  try {
    s.enabled ? await api.disableService(s.service_id) : await api.enableService(s.service_id)
    await load()
  } catch { /* toast 已展示 */ }
  finally { togglingId.value = '' }
}

async function probe(s) {
  probing.value = s.service_id
  try { await api.probeService(s.service_id); await load() }
  catch { /* toast 已展示 */ }
  finally { probing.value = '' }
}

// --- 全部检测（BUG-02 并发限流队列：限制槽数为 3）---
async function probeAll() {
  probingAll.value = true
  try {
    const list = (await api.listServices('admin')).filter(s => s.enabled)
    const concurrency = 3
    let index = 0
    async function worker() {
      while (index < list.length) {
        const s = list[index++]
        if (!s) break
        probing.value = s.service_id
        try {
          await api.probeService(s.service_id)
        } catch { /* ignore */ }
      }
    }
    const workers = Array.from({ length: Math.min(concurrency, list.length) }, () => worker())
    await Promise.all(workers)
    await load()
  } finally {
    probing.value = ''
    probingAll.value = false
  }
}

async function copyUrl(s) {
  try {
    await navigator.clipboard.writeText(displayUrl(s))
    copied.value = s.service_id
    setTimeout(() => { if (copied.value === s.service_id) copied.value = '' }, 1500)
  } catch { /* 静默降级 */ }
}

// --- 展示映射与统计 ---
const HEALTH_META = {
  HEALTHY:     { text: '健康',     dot: 'bg-up',    cls: 'badge badge-ok' },
  DEGRADED:    { text: '响应较慢', dot: 'bg-warn',  cls: 'badge badge-warn' },
  UNREACHABLE: { text: '不可达',   dot: 'bg-brand', cls: 'badge badge-err' },
  UNKNOWN:     { text: '未检测',   dot: 'bg-t3',    cls: 'badge badge-gray' },
}
const health = (h) => HEALTH_META[h] || HEALTH_META.UNKNOWN
// HIAGENT 的「健康」是连通性测试（建会话+发消息），与 A2A 的 Card 探测口径不同
const healthText = (s) => isHiagent(s) ? `${health(s.health).text}（连通性）` : health(s.health).text

const COMPAT_META = {
  COMPATIBLE:   { text: '兼容',   cls: 'badge badge-ok' },
  VERIFYING:    { text: '校验中', cls: 'badge badge-warn' },
  INCOMPATIBLE: { text: '不兼容', cls: 'badge badge-err' },
}
const compat = (c) => COMPAT_META[c] || { text: c || '未知', cls: 'badge badge-gray' }

const dirty = computed(() =>
  !!search.value.trim() || tile.value !== 'all' || enabledFilter.value !== 'all')

function reset() {
  search.value = ''
  tile.value = 'all'
  enabledFilter.value = 'all'
}
function pickTile(k) { tile.value = tile.value === k ? 'all' : k }

// Esc 监听
function onKey(e) {
  if (e.key !== 'Escape') return
  if (drawerTarget.value) closeDrawer()
  else if (deleteTarget.value) closeDelete()
  else if (showForm.value) closeForm()
  else if (credTarget.value) closeCred()
}
watch([showForm, credTarget, editTarget, deleteTarget, drawerTarget], ([f, c, ed, del, dr]) => {
  document.body.style.overflow = (f || c || ed || del || dr) ? 'hidden' : ''
})
onMounted(() => window.addEventListener('keydown', onKey))
onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKey)
  document.body.style.overflow = ''
})
</script>

<template>
  <div>
    <!-- 页头 -->
    <div class="flex items-start justify-between mb-5">
      <div>
        <h1 class="text-xl font-semibold">服务注册</h1>
        <p class="text-t2 mt-1">把外部 A2A 服务或 HIA 智能体登记进统一目录：A2A 服务自动拉取名片并校验兼容性；HIA 智能体由平台转换成标准单 Agent A2A 服务对外暴露。</p>
      </div>
      <div class="flex gap-2 shrink-0">
        <button v-if="canManage" class="btn btn-ghost" :disabled="probingAll || !stats.enabled" @click="probeAll">
          <svg class="w-4 h-4" :class="probingAll && 'animate-spin'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M21 12a9 9 0 11-2.64-6.36L21 8"/><path d="M21 3v5h-5"/></svg>
          {{ probingAll ? '顺序检测中…' : '全部检测' }}
        </button>
        <button v-if="canManage" class="btn btn-primary" @click="openForm">
          <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M12 5v14M5 12h14"/></svg>注册服务
        </button>
      </div>
    </div>

    <div v-if="err" class="card !py-4 flex items-center gap-2 text-brand">
      <svg class="w-4 h-4 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/></svg>
      {{ err }}（请确认后端已启动）
    </div>

    <!-- 概览：点击卡片即筛选 -->
    <div class="grid grid-cols-4 gap-6 mb-6">
      <button
        :class="['bg-white rounded-xl shadow-sm p-5 text-left border transition-colors',
                 tile === 'all' ? 'border-brand' : 'border-transparent hover:border-line']"
        @click="tile = 'all'">
        <div class="flex justify-between items-start">
          <span class="text-sm text-t2">注册服务</span>
          <div class="w-10 h-10 rounded-[10px] bg-brand-light text-brand flex items-center justify-center">
            <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M4 7h16M4 12h16M4 17h16"/><circle cx="8" cy="7" r="1.6"/><circle cx="13" cy="12" r="1.6"/><circle cx="8" cy="17" r="1.6"/></svg>
          </div>
        </div>
        <div class="text-[34px] font-bold mt-2.5 leading-none">{{ stats.total }}</div>
        <div class="mt-3 text-[13px] text-t3">已启用 {{ stats.enabled }} · 已停用 {{ stats.disabled }}</div>
      </button>

      <button
        :class="['bg-white rounded-xl shadow-sm p-5 text-left border transition-colors',
                 tile === 'healthy' ? 'border-brand' : 'border-transparent hover:border-line']"
        @click="pickTile('healthy')">
        <div class="flex justify-between items-start">
          <span class="text-sm text-t2">健康</span>
          <div class="w-10 h-10 rounded-[10px] bg-up-light text-up flex items-center justify-center">
            <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M9 12l2 2 4-4"/><circle cx="12" cy="12" r="9"/></svg>
          </div>
        </div>
        <div class="text-[34px] font-bold mt-2.5 leading-none">{{ stats.healthy }}</div>
        <div class="mt-3 text-[13px] text-t3">
          {{ stats.avgLatency == null ? '暂无延迟样本' : `平均延迟 ${stats.avgLatency} ms` }}
        </div>
      </button>

      <button
        :class="['bg-white rounded-xl shadow-sm p-5 text-left border transition-colors',
                 tile === 'abnormal' ? 'border-brand' : 'border-transparent hover:border-line']"
        @click="pickTile('abnormal')">
        <div class="flex justify-between items-start">
          <span class="text-sm text-t2">异常</span>
          <div class="w-10 h-10 rounded-[10px] bg-warn-light text-warn flex items-center justify-center">
            <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9L2.6 17a2 2 0 001.7 3h15.4a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z"/></svg>
          </div>
        </div>
        <div class="text-[34px] font-bold mt-2.5 leading-none">{{ stats.abnormal }}</div>
        <div class="mt-3 text-[13px] text-t3">不可达 {{ stats.unreachable }} · 响应慢 {{ stats.degraded }} · 未检测 {{ stats.unknown }}</div>
      </button>

      <button
        :class="['bg-white rounded-xl shadow-sm p-5 text-left border transition-colors',
                 tile === 'incompat' ? 'border-brand' : 'border-transparent hover:border-line']"
        @click="pickTile('incompat')">
        <div class="flex justify-between items-start">
          <span class="text-sm text-t2">协议兼容性</span>
          <div class="w-10 h-10 rounded-[10px] bg-warn-light text-warn flex items-center justify-center">
            <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M14 7h3a5 5 0 010 10h-3M10 17H7A5 5 0 017 7h3M8 12h8"/></svg>
          </div>
        </div>
        <div class="text-[34px] font-bold mt-2.5 leading-none">{{ stats.compatRisk }}</div>
        <div class="mt-3 text-[13px] text-t3">兼容 {{ stats.compatible }} / 共 {{ stats.total }}</div>
      </button>
    </div>

    <!-- 列表 -->
    <div class="card !p-0">
      <div class="flex items-center gap-3 px-5 py-4 border-b border-line">
        <div class="flex items-center gap-2 border border-line rounded-lg px-3 bg-white w-80 focus-within:border-brand transition-colors">
          <svg class="w-4 h-4 text-t3 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>
          <input v-model="search" placeholder="搜索服务名称 / 标识 / 标签 / 地址" class="h-9 flex-1 outline-none text-sm bg-transparent" />
          <button v-if="search" class="text-t3 hover:text-t1 shrink-0" @click="search = ''">
            <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M18 6L6 18M6 6l12 12"/></svg>
          </button>
        </div>

        <select v-model="enabledFilter" class="h-9 px-2.5 border border-line rounded-lg text-sm bg-white text-t1 outline-none focus:border-brand cursor-pointer">
          <option value="all">全部状态</option>
          <option value="on">已启用</option>
          <option value="off">已停用</option>
        </select>

        <button v-if="dirty" class="btn btn-text" @click="reset">重置筛选</button>

        <div class="ml-auto text-[13px] text-t3">
          共 <span class="text-t1 font-medium">{{ servicesPager.total }}</span> 个服务
          <span v-if="dirty">（已筛选，总数 {{ stats.total }}）</span>
        </div>
      </div>

      <div class="overflow-x-auto">
        <table class="tbl">
          <thead>
            <tr>
              <th class="!pl-5">服务</th>
              <th class="w-36">兼容性</th>
              <th class="w-48">健康状态</th>
              <th class="w-24">启用</th>
              <!-- BUG-06 修复：操作列宽度扩大自适应，杜绝按钮换行 -->
              <th class="min-w-[320px] !pr-5 text-right">操作</th>
            </tr>
          </thead>
          <tbody>
            <!-- 骨架屏 -->
            <template v-if="loading">
              <tr v-for="n in 3" :key="`sk-${n}`">
                <td class="!pl-5"><div class="h-4 w-40 rounded bg-canvas animate-pulse"></div><div class="h-3 w-64 rounded bg-canvas animate-pulse mt-2"></div></td>
                <td><div class="h-5 w-16 rounded bg-canvas animate-pulse"></div></td>
                <td><div class="h-5 w-20 rounded bg-canvas animate-pulse"></div></td>
                <td><div class="h-5 w-9 rounded-full bg-canvas animate-pulse"></div></td>
                <td class="!pr-5"><div class="h-5 w-48 rounded bg-canvas animate-pulse ml-auto"></div></td>
              </tr>
            </template>

            <template v-else>
            <tr v-for="s in services" :key="s.service_id" class="align-top hover:bg-canvas/50 transition-colors group">
              <!-- 服务：名称 / 标识 / 来源 / 标签 / 地址 -->

              <td class="!pl-5">
                <div class="flex items-center gap-2">
                  <span class="font-semibold text-t1 hover:text-brand cursor-pointer" @click="openDrawer(s)">
                    {{ s.display_name || s.slug }}
                  </span>
                  <span class="mono text-[12px] bg-canvas text-t2 px-1.5 py-0.5 rounded">{{ s.slug }}</span>
                  <span v-if="s.tags" class="text-[11px] bg-brand-light text-brand px-1.5 py-0.5 rounded">
                    {{ s.tags }}
                  </span>
                  <span v-if="isHiagent(s)" class="badge badge-brand" title="HiAgent 来源：平台生成名片并挂载 /a2a/{slug}/，健康口径为连通性测试">HiAgent</span>
                </div>
                <!-- BUG-01 修复：规范化 Card URL 展示与复制 -->
                <div class="flex items-center gap-1.5 mt-1.5 group">
                  <svg class="w-3.5 h-3.5 text-t3 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M10 13a5 5 0 007.5.5l3-3a5 5 0 00-7-7l-1.7 1.7"/><path d="M14 11a5 5 0 00-7.5-.5l-3 3a5 5 0 007 7l1.7-1.7"/></svg>
                  <span class="mono text-[12px] truncate max-w-[26rem]" :title="displayUrl(s)">{{ displayUrl(s) }}</span>
                  <button class="text-t3 hover:text-brand opacity-0 group-hover:opacity-100 transition-opacity shrink-0"
                          :title="copied === s.service_id ? '已复制' : '复制地址'" @click="copyUrl(s)">
                    <svg v-if="copied === s.service_id" class="w-3.5 h-3.5 text-up" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6L9 17l-5-5"/></svg>
                    <svg v-else class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 012-2h10"/></svg>
                  </button>
                </div>
              </td>

              <!-- 兼容性 -->
              <td>
                <span :class="compat(s.compatibility).cls">{{ compat(s.compatibility).text }}</span>
                <div v-if="s.compatibility_reason" class="text-[12px] text-t3 mt-1 truncate max-w-[14rem]" :title="s.compatibility_reason">
                  {{ s.compatibility_reason }}
                </div>
              </td>

              <!-- 健康状态 -->
              <td class="whitespace-nowrap">
                <div class="flex items-center gap-1.5">
                  <span class="w-2 h-2 rounded-full shrink-0" :class="health(s.health).dot"></span>
                  <span class="text-sm text-t1 font-medium">{{ healthText(s) }}</span>
                  <span v-if="s.health_latency_ms != null" class="mono text-[12px] text-t3 ml-1">
                    {{ s.health_latency_ms }} ms
                  </span>
                </div>
                <div v-if="s.health_observed_at" class="text-[12px] text-t3 mt-1">
                  {{ fmtTime(s.health_observed_at) }}
                </div>
              </td>

              <!-- 启用状态（防抖锁） -->
              <td>
                <button
                  class="relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full transition-colors duration-200 ease-in-out focus:outline-none disabled:opacity-50"
                  :class="s.enabled ? 'bg-up' : 'bg-line'"
                  :disabled="togglingId === s.service_id"
                  @click="toggle(s)">
                  <span
                    class="pointer-events-none inline-block h-4 w-4 transform rounded-full bg-white shadow-sm ring-0 transition duration-200 ease-in-out mt-0.5"
                    :class="s.enabled ? 'translate-x-4 ml-0.5' : 'translate-x-0.5'" />
                </button>
              </td>

              <!-- 操作列 -->
              <td class="!pr-5 whitespace-nowrap text-right">
                <div class="inline-flex items-center justify-end gap-1">
                  <button v-if="canManage && isHiagent(s)" class="btn btn-text" :disabled="!s.enabled" @click="runService = s">试运行</button>
                  <button class="btn btn-text text-t2 hover:text-brand" title="查看卡片详情与技能" @click="openDrawer(s)">卡片</button>
                  <button v-if="canManage" class="btn btn-text" :disabled="probing === s.service_id || probingAll" @click="probe(s)">
                    {{ probing === s.service_id ? '检测中…' : '检测' }}
                  </button>
                  <button v-if="canManage" class="btn btn-text" :disabled="refreshing === s.service_id" @click="refresh(s)">
                    {{ refreshing === s.service_id ? '刷新中…' : (isHiagent(s) ? '重建名片' : '刷新 Card') }}
                  </button>
                  <button v-if="canManage" class="btn btn-text" :class="s.outbound_credential_configured && '!text-brand font-medium'" @click="openCred(s)">
                    {{ s.outbound_credential_configured ? '凭证·已配' : '凭证' }}
                  </button>
                  <button v-if="canManage" class="btn btn-text text-brand hover:underline" @click="openEdit(s)">编辑</button>
                  <button v-if="canManage" class="btn btn-text text-down hover:underline" @click="openDelete(s)">删除</button>
                </div>
              </td>
            </tr>

            <tr v-if="!services.length">
              <td colspan="5" class="py-12 text-center">
                <div class="flex flex-col items-center">
                  <div class="w-12 h-12 rounded-full bg-canvas text-t3 flex items-center justify-center mb-3">
                    <svg class="w-6 h-6" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>
                  </div>
                  <template v-if="stats.total">
                    <div class="text-t1 font-medium">没有匹配的服务</div>
                    <div class="text-t3 mt-1">试试调整搜索词或筛选条件</div>
                    <button class="btn btn-ghost btn-sm mt-4" @click="reset">重置筛选</button>
                  </template>
                  <template v-else>
                    <div class="text-t1 font-medium">还没有注册任何服务</div>
                    <div class="text-t3 mt-1">外部 A2A 服务填地址即可（自动拉取名片校验）；HIA 智能体选 HiAgent 来源登记，平台会转换成标准 A2A 服务暴露</div>
                    <button v-if="canManage" class="btn btn-primary btn-sm mt-4" @click="openForm">注册第一个服务</button>
                  </template>
                </div>
              </td>
            </tr>
            </template>
          </tbody>
        </table>
        <Pagination :pager="servicesPager" />
      </div>
    </div>

    <!-- 1. 注册服务弹窗 -->
    <div v-if="showForm" class="fixed inset-0 bg-black/30 backdrop-blur-[1px] flex items-center justify-center z-50 p-4" @click.self="closeForm">
      <div class="bg-white rounded-xl w-[560px] shadow-xl overflow-hidden max-h-[92vh] flex flex-col">
        <div class="flex items-center justify-between px-6 py-4 border-b border-line">
          <h3 class="font-semibold text-base">{{ editTarget ? (formSource === 'HIAGENT' ? '编辑 HiAgent 智能体' : '编辑外部 A2A 服务') : '注册服务' }}</h3>
          <button class="text-t3 hover:text-t1" @click="closeForm">
            <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M18 6L6 18M6 6l12 12"/></svg>
          </button>
        </div>

        <div class="px-6 py-5 overflow-y-auto">
          <p v-if="editLoading" class="text-sm text-t3 mb-4">正在读取已注册配置…</p>
          <div v-if="editError" class="text-sm text-brand mb-4">{{ editError }} <button class="btn btn-text" @click="loadEdit">重试</button></div>
          <fieldset :disabled="busy || editLoading || !!editError" class="contents">
          <!-- 来源切换：A2A 远端服务 / HiAgent 智能体 -->
          <div class="mb-4">
            <label class="block text-[13px] text-t2 mb-1.5">注册来源 <span class="text-brand">*</span></label>
            <div class="grid grid-cols-2 gap-2">
              <button :class="['h-9 rounded-lg border text-[13px] transition-colors',
                               formSource==='A2A' ? 'border-brand bg-brand-light text-brand font-medium' : 'border-line text-t2 hover:border-brand']"
                      :disabled="!!editTarget" @click="formSource = 'A2A'">外部 A2A 服务</button>
              <button :class="['h-9 rounded-lg border text-[13px] transition-colors',
                               formSource==='HIAGENT' ? 'border-brand bg-brand-light text-brand font-medium' : 'border-line text-t2 hover:border-brand']"
                      :disabled="!!editTarget" @click="formSource = 'HIAGENT'">HIA 智能体（HiAgent）</button>
            </div>
            <p class="text-xs text-t3 mt-1.5">
              {{ formSource === 'A2A'
                ? '填服务地址，系统自动拉取 /.well-known/agent-card.json 名片并校验兼容性'
                : '平台把 HIA 智能体转换成标准单 Agent A2A 服务挂载到 /a2a/{slug}/；注册即做连通性测试（建会话 + 发「你好」）' }}
            </p>
          </div>

          <div class="mb-4">
            <label class="block text-[13px] text-t2 mb-1.5">服务标识 <span class="text-brand">*</span></label>
            <input v-model="form.slug" :disabled="!!editTarget" class="input" :class="slugError && '!border-brand'" placeholder="finance" @keydown.enter="submit" />
            <p :class="['text-xs mt-1.5', slugError ? 'text-brand' : 'text-t3']">
              {{ editTarget ? '服务标识和注册来源创建后不可修改' : slugError || (formSource === 'HIAGENT'
                ? `全局唯一（含与 Agent Team 互斥）；注册成功后暴露 ${a2aBase}/a2a/${form.slug.trim() || '<slug>'}/`
                : '全局唯一（含与 Agent Team 互斥），用于绑定与调用寻址；小写字母、数字与短横线') }}
            </p>
          </div>
          <div class="mb-4">
            <label class="block text-[13px] text-t2 mb-1.5">服务名称
              <span v-if="formSource === 'HIAGENT'" class="text-brand">*</span>
            </label>
            <input v-model="form.displayName" class="input" placeholder="财务分析师" @keydown.enter="submit" />
            <p class="text-xs text-t3 mt-1.5">
              {{ formSource === 'A2A' ? '留空则用对方名片里自带的名称' : '展示在目录与生成式名片上' }}
            </p>
          </div>

          <!-- A2A 来源：Base URL + 标签 + 出站凭证 -->
          <template v-if="formSource === 'A2A'">
            <div class="mb-4">
            <label class="block text-[13px] text-t2 mb-1.5">服务 Base URL <span class="text-brand">*</span></label>
            <div class="flex gap-2">
              <input v-model="form.agentCardUrl" class="input flex-1" :class="urlError && '!border-brand'" placeholder="http://finance.agents.example:9101" />
              <button class="btn btn-ghost shrink-0 text-brand" :disabled="formProbeBusy || !form.agentCardUrl || !!urlError" @click="probeFormUrl">
                <svg v-if="formProbeBusy" class="w-3.5 h-3.5 animate-spin mr-1" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M21 12a9 9 0 11-2.64-6.36"/></svg>
                {{ formProbeBusy ? '探测中' : '测试连接' }}
              </button>
            </div>
            <p :class="['text-xs mt-1.5', urlError ? 'text-brand' : 'text-t3']">
              {{ urlError || '填服务主机与端口即可，系统会自动寻址 /.well-known/agent-card.json' }}
            </p>
            <!-- 预检卡片摘要 -->
            <div v-if="formProbeResult" class="mt-2.5 p-3 bg-canvas rounded-lg text-xs border border-line">
              <div class="flex items-center justify-between">
                <span class="font-medium text-t1">{{ formProbeResult.card?.name || '未知名称' }}</span>
                <span class="badge" :class="formProbeResult.compatibility === 'COMPATIBLE' ? 'badge-ok' : 'badge-err'">
                  {{ formProbeResult.compatibility }}
                </span>
              </div>
              <div class="text-t3 mt-1">{{ formProbeResult.card?.description || '未声明描述' }}</div>
              <div class="mt-1.5 text-t2">
                技能数：{{ formProbeResult.card?.skills?.length || 0 }} 个 · 协议：{{ formProbeResult.card?.protocolVersion || '0.2.x' }}
              </div>
            </div>
          </div>

            <!-- A2A：业务标签 -->
            <div class="mb-4">
              <label class="block text-[13px] text-t2 mb-1.5">业务标签 (Tags)</label>
              <input v-model="form.tags" class="input" placeholder="office,finance,dev" />
              <p class="text-xs text-t3 mt-1.5">可选，逗号分隔，方便在列表中按分类筛选搜索</p>
            </div>

            <!-- A2A：出站凭证 -->
            <div class="mb-4">
              <label class="block text-[13px] text-t2 mb-1.5">API Key</label>
              <input v-model="form.outboundApiKey" type="password" autocomplete="new-password"
                class="input" :disabled="clearCredential" :placeholder="editTarget ? '留空保留原 API Key，填写则替换' : '填写外部 A2A 服务提供的 API Key'" />
              <p class="text-xs text-t3 mt-1.5">{{ editTarget ? (credentialConfigured ? '已配置 API Key，留空保留原值。' : '尚未配置 API Key。') : '可选。调用该服务时自动放入 apikey 请求头，加密存储且不回显。' }}</p>
            </div>
          </template>

          <!-- HIAGENT 来源：实例级地址 / AppKey / 描述 / 技能 -->
          <template v-else>
            <div class="mb-4">
              <label class="block text-[13px] text-t2 mb-1.5">HiAgent 服务地址</label>
              <input v-model="form.baseUrl" class="input" :class="baseUrlError && '!border-brand'" placeholder="留空使用全局默认（HIAGENT_URL）" @keydown.enter="submit" />
              <p :class="['text-xs mt-1.5', baseUrlError ? 'text-brand' : 'text-t3']">
                {{ baseUrlError || '实例级覆盖：每个 HIA 智能体可指向独立的 hiagent 服务地址，留空跟随全局默认' }}
              </p>
            </div>
            <div class="mb-4">
              <label class="block text-[13px] text-t2 mb-1.5">AppKey</label>
              <input v-model="form.appKey" type="password" autocomplete="new-password" class="input" :disabled="clearCredential" :placeholder="editTarget ? '留空保留原 AppKey，填写则替换' : '留空使用全局默认（HIAGENT_API_KEY）'" @keydown.enter="submit" />
              <p class="text-xs text-t3 mt-1.5">{{ editTarget ? (credentialConfigured ? '已配置实例 AppKey，留空保留原值。' : '使用全局默认 AppKey。') : '实例级凭证，AES-GCM 加密落库、永不回显；留空跟随全局默认' }}</p>
            </div>
            <div class="mb-4">
              <label class="block text-[13px] text-t2 mb-1.5">描述</label>
              <textarea v-model="form.description" rows="2" class="input resize-none" placeholder="这个智能体负责什么"></textarea>
              <p class="text-xs text-t3 mt-1.5">写入平台生成式名片，让调用方知道它管什么</p>
            </div>
            <div>
              <div class="flex items-center justify-between mb-1.5">
                <label class="block text-[13px] text-t2">技能清单</label>
                <button class="btn btn-text !h-7" @click="addSkill">+ 添加技能</button>
              </div>
              <div v-for="(sk, i) in skills" :key="i" class="flex items-center gap-2 mb-2">
                <input v-model="sk.name" class="input flex-1" placeholder="技能名（如 差旅政策问答）" />
                <input v-model="sk.description" class="input flex-[1.4]" placeholder="技能说明（可选）" />
                <button class="!h-7 px-1 text-t3 hover:text-brand shrink-0" title="移除该技能" @click="removeSkill(i)">
                  <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M18 6L6 18M6 6l12 12"/></svg>
                </button>
              </div>
              <p class="text-xs text-t3 mt-1.5">可选；作为生成式名片的 skills 素材，也提示调用方该智能体的能力面</p>
            </div>
            <section class="mt-5 border-t border-line pt-4" aria-label="业务参数设置">
              <div class="flex items-center justify-between gap-3 mb-2">
                <h3 class="text-[13px] font-medium">业务参数 <span class="text-t3 font-normal">（选填）</span></h3>
                <button type="button" class="btn btn-text !h-7" :disabled="busy || parameterRows.length >= 128" @click="addParameter">+ 添加参数</button>
              </div>
              <p class="text-xs text-t3 leading-5 mb-3">仅填写这个智能体需要的额外信息。用户身份会自动传入，AppKey 填在上方。</p>
              <div v-if="!parameterRows.length" class="rounded-lg border border-dashed border-line bg-canvas px-4 py-3 text-[13px] text-t2">
                暂无额外参数。
              </div>
              <div v-for="(parameter, index) in parameterRows" :key="parameter.id" class="rounded-lg border border-line p-3 mb-3">
                <div class="flex items-center justify-between mb-2">
                  <span class="text-xs text-t3">参数 {{ index + 1 }}</span>
                  <div class="flex items-center gap-3">
                    <label class="flex items-center gap-1.5 text-xs text-t2"><input v-model="parameter.required" type="checkbox" :disabled="busy" />必填</label>
                    <button type="button" class="text-xs text-t3 hover:text-brand" :disabled="busy" :aria-label="`删除参数 ${index + 1}`" @click="parameterRows.splice(index, 1)">删除</button>
                  </div>
                </div>
                <div class="grid grid-cols-[minmax(0,1fr)_110px] gap-3">
                  <label class="block text-xs text-t2">参数名
                    <input v-model="parameter.name" class="input w-full mt-1" placeholder="例如 tenant_id" :disabled="busy" :aria-describedby="`parameter-error-${parameter.id}`" />
                  </label>
                  <label class="block text-xs text-t2">类型
                    <select v-model="parameter.type" class="input w-full mt-1" :disabled="busy" @change="parameter.testValue = ''">
                      <option value="string">文字</option><option value="number">数字</option><option value="integer">整数</option><option value="boolean">是 / 否</option>
                      <option v-if="parameter.originalProperty?.type === 'object'" value="object">对象（JSON）</option>
                      <option v-if="parameter.originalProperty?.type === 'array'" value="array">数组（JSON）</option>
                    </select>
                  </label>
                </div>
                <label class="block text-xs text-t2 mt-3">测试值 <span class="text-t3">{{ parameter.required ? '（注册时用于验证连接）' : '（选填，留空则不传）' }}</span>
                  <select v-if="parameter.type === 'boolean'" v-model="parameter.testValue" class="input w-full mt-1" :disabled="busy">
                    <option value="">请选择</option><option value="true">是</option><option value="false">否</option>
                  </select>
                  <select v-else-if="parameter.type === 'string' && parameterChoices(parameter).length" v-model="parameter.testValue" class="input w-full mt-1" :disabled="busy">
                    <option value="">请选择</option><option v-for="choice in parameterChoices(parameter)" :key="choice" :value="choice">{{ choice }}</option>
                  </select>
                  <input v-else v-model="parameter.testValue" class="input w-full mt-1" :inputmode="['number', 'integer'].includes(parameter.type) ? 'decimal' : 'text'" :placeholder="['object', 'array'].includes(parameter.type) ? '填写 JSON 对象或数组' : parameter.type === 'string' ? '填写真实可用的值，例如租户编号' : '填写数字，例如 0 或 10'" :disabled="busy" :aria-describedby="`parameter-error-${parameter.id}`" />
                </label>
                <details class="mt-2 text-xs text-t3">
                  <summary class="cursor-pointer">说明与可选值（选填）</summary>
                  <label class="block mt-2">参数说明<input v-model="parameter.description" class="input w-full mt-1" placeholder="例如：租户编号" :disabled="busy" /></label>
                  <label v-if="parameter.type === 'string'" class="block mt-2">限定可选值<input v-model="parameter.options" class="input w-full mt-1" placeholder="例如 P1, P2, P3；不限制则留空" :disabled="busy" /></label>
                </details>
                <p :id="`parameter-error-${parameter.id}`" class="text-xs text-brand mt-2" aria-live="polite">{{ parameterConfig.errors[index] || '' }}</p>
              </div>
              <label class="block text-[13px] text-t2 mt-4" for="hiagent-test-query">测试问题</label>
              <input id="hiagent-test-query" v-model="testQuery" class="input w-full mt-1" placeholder="例如：查询差旅政策" :disabled="busy" />
              <p class="text-xs text-t3 mt-1.5">保存前会发送这句话，确认智能体能够响应。</p>
              <p v-if="parameterConfig.queryError" class="text-xs text-brand mt-1" aria-live="polite">{{ parameterConfig.queryError }}</p>
              <details class="mt-3 text-xs text-t3">
                <summary class="cursor-pointer">查看自动生成的参数规则</summary>
                <pre class="mt-2 rounded-lg bg-canvas p-3 whitespace-pre-wrap break-all max-h-48 overflow-auto">{{ JSON.stringify(parameterConfig.schema, null, 2) }}</pre>
              </details>
            </section>
          </template>
          <label v-if="editTarget && credentialConfigured" class="flex items-center gap-2 text-sm text-t2 mt-4">
            <input v-model="clearCredential" type="checkbox" />{{ formSource === 'HIAGENT' ? '清除实例 AppKey，改用全局默认' : '清除已保存的 API Key' }}
          </label>
          </fieldset>
        </div>

        <div class="flex justify-end gap-2 px-6 py-4 bg-canvas border-t border-line">
          <button class="btn btn-ghost" :disabled="busy" @click="closeForm">取消</button>
          <button class="btn btn-primary" :disabled="busy || !formValid" @click="submit">
            <svg v-if="busy" class="w-4 h-4 animate-spin mr-1" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M21 12a9 9 0 11-2.64-6.36"/></svg>
            {{ busy ? (formSource === 'HIAGENT' ? '连通性测试中…' : '保存中…') : editTarget ? '保存修改' : '注册' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 3. 删除服务高危确认弹窗 -->
    <div v-if="deleteTarget" class="fixed inset-0 bg-black/40 backdrop-blur-[1px] flex items-center justify-center z-50 p-4" @click.self="closeDelete">
      <div class="bg-white rounded-xl w-[520px] shadow-2xl overflow-hidden border border-down/30">
        <div class="flex items-center justify-between px-6 py-4 border-b border-line bg-down-light/20">
          <div class="flex items-center gap-2 text-down font-semibold">
            <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9L2.6 17a2 2 0 001.7 3h15.4a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z"/></svg>
            <span>删除 A2A 服务</span>
          </div>
          <button class="text-t3 hover:text-t1" @click="closeDelete">
            <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M18 6L6 18M6 6l12 12"/></svg>
          </button>
        </div>

        <div class="px-6 py-5 space-y-4">
          <p class="text-sm text-t1">
            确定要彻底注销并删除服务 <strong class="text-down font-mono">「{{ deleteTarget.display_name || deleteTarget.slug }}」</strong> 吗？
          </p>

          <div class="p-3 bg-canvas rounded-lg text-xs text-t2 space-y-1.5 border border-line">
            <div class="font-medium text-t1">级联清理范围（不可撤销）：</div>
            <div>• 清理服务主记录及其所有的 Agent Card 版本快照</div>
            <div>• 清理历史健康观测数据及实例发现租约</div>
            <div>• 清理发布版本 (Releases) 及工作空间安装绑定</div>
            <div>• 清理用户及权限组对此服务的授权映射</div>
          </div>

          <!-- 总控绑定警示 -->
          <div v-if="checkingBindings" class="text-xs text-t3 flex items-center gap-1.5">
            <svg class="w-3.5 h-3.5 animate-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M21 12a9 9 0 11-2.64-6.36"/></svg>
            正在排查总控智能体依赖关系…
          </div>
          <div v-else-if="deleteBoundMasters.length > 0" class="p-3 bg-warn-light/40 border border-warn/30 rounded-lg text-xs">
            <div class="font-medium text-warn flex items-center gap-1">
              <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9L2.6 17a2 2 0 001.7 3h15.4a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z"/></svg>
              总控绑定依赖警告
            </div>
            <div class="mt-1 text-t1">
              该服务已被以下总控智能体作为工具绑定：<span class="font-mono font-semibold">{{ deleteBoundMasters.join(', ') }}</span>。
            </div>
            <label class="flex items-center gap-2 mt-2.5 cursor-pointer text-down font-medium">
              <input type="checkbox" v-model="deleteForce" class="rounded text-down focus:ring-down" />
              <span>同时解除总控绑定并在运行时剔除该技能 (force=true)</span>
            </label>
          </div>

          <div>
            <label class="block text-xs text-t2 mb-1.5">
              请输入服务标识 <span class="font-mono font-bold text-t1 select-all bg-canvas px-1 rounded">{{ deleteTarget.slug }}</span> 以确认操作：
            </label>
            <input v-model="deleteConfirmSlug" class="input mono text-sm" :placeholder="deleteTarget.slug" />
          </div>
        </div>

        <div class="flex justify-end gap-2 px-6 py-4 bg-canvas border-t border-line">
          <button class="btn btn-ghost" :disabled="deleteBusy" @click="closeDelete">取消</button>
          <button
            class="btn bg-down text-white hover:bg-down/90 disabled:opacity-40"
            :disabled="deleteBusy || deleteConfirmSlug.trim() !== deleteTarget.slug || (deleteBoundMasters.length > 0 && !deleteForce)"
            @click="confirmDelete">
            <svg v-if="deleteBusy" class="w-4 h-4 animate-spin mr-1" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 11-2.64-6.36"/></svg>
            {{ deleteBusy ? '正在清理…' : '确认彻底删除' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 4. 卡片详情抽屉 (Card Drawer) -->
    <div v-if="drawerTarget" class="fixed inset-0 bg-black/30 backdrop-blur-[1px] z-50 flex justify-end" @click.self="closeDrawer">
      <div class="bg-white w-[540px] h-full shadow-2xl flex flex-col overflow-hidden animate-in slide-in-from-right duration-200">
        <div class="flex items-center justify-between px-6 py-4 border-b border-line bg-canvas">
          <div>
            <h3 class="font-semibold text-base">{{ drawerTarget.display_name || drawerTarget.slug }}</h3>
            <p class="text-xs text-t3 font-mono mt-0.5">{{ drawerTarget.slug }} · {{ drawerTarget.agent_card_url }}</p>
          </div>
          <button class="text-t3 hover:text-t1" @click="closeDrawer">
            <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M18 6L6 18M6 6l12 12"/></svg>
          </button>
        </div>

        <div class="flex-1 overflow-y-auto p-6 space-y-5">
          <div v-if="drawerLoading" class="flex items-center justify-center py-20 text-t3">
            <svg class="w-6 h-6 animate-spin mr-2" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 11-2.64-6.36"/></svg>
            加载 Card 详情中…
          </div>
          <template v-else-if="drawerData">
            <!-- 基础契约指标 -->
            <div class="grid grid-cols-2 gap-3">
              <div class="p-3 bg-canvas rounded-lg">
                <div class="text-[12px] text-t3">协议版本</div>
                <div class="text-sm font-semibold mt-1">{{ drawerData.card?.protocolVersion || '0.2.x' }}</div>
              </div>
              <div class="p-3 bg-canvas rounded-lg">
                <div class="text-[12px] text-t3">流式能力 (Streaming)</div>
                <div class="text-sm font-semibold mt-1">{{ drawerData.card?.capabilities?.streaming ? '支持' : '不支持' }}</div>
              </div>
            </div>

            <!-- 技能清单 (Skills) -->
            <div>
              <div class="flex items-center justify-between mb-2.5">
                <h4 class="font-semibold text-sm">提供技能 ({{ drawerData.card?.skills?.length || 0 }})</h4>
              </div>
              <div class="space-y-2.5">
                <div v-for="skill in (drawerData.card?.skills || [])" :key="skill.id || skill.name" class="p-3.5 border border-line rounded-lg bg-white shadow-xs">
                  <div class="flex items-center justify-between">
                    <span class="font-semibold text-sm text-t1 font-mono">{{ skill.name || skill.id }}</span>
                    <span v-if="skill.id && skill.id !== skill.name" class="text-[11px] text-t3 font-mono">{{ skill.id }}</span>
                  </div>
                  <p class="text-xs text-t2 mt-1.5 leading-relaxed">{{ skill.description || '无技能描述' }}</p>
                  <div v-if="skill.tags?.length" class="flex flex-wrap gap-1.5 mt-2">
                    <span v-for="t in skill.tags" :key="t" class="text-[10px] bg-canvas text-t3 px-1.5 py-0.5 rounded">
                      #{{ t }}
                    </span>
                  </div>
                </div>
                <div v-if="!drawerData.card?.skills?.length" class="text-xs text-t3 p-4 text-center border border-dashed rounded-lg">
                  该 Agent Card 未声明任何技能
                </div>
              </div>
            </div>

            <!-- Card 原始 JSON -->
            <div>
              <h4 class="font-semibold text-sm mb-2">Agent Card 原始快照</h4>
              <pre class="bg-canvas border border-line rounded-lg p-3 text-[11px] font-mono text-t2 overflow-x-auto max-h-60">{{ JSON.stringify(drawerData.card, null, 2) }}</pre>
            </div>
          </template>
        </div>
      </div>
    </div>

    <!-- 5. 出站凭证编辑弹窗 -->
    <div v-if="credTarget" class="fixed inset-0 bg-black/30 backdrop-blur-[1px] flex items-center justify-center z-50 p-4" @click.self="closeCred">
      <div class="bg-white rounded-xl w-[520px] shadow-xl overflow-hidden">
        <div class="flex items-center justify-between px-6 py-4 border-b border-line">
          <h3 class="font-semibold text-base">出站凭证 · {{ credTarget.display_name || credTarget.slug }}</h3>
          <button class="text-t3 hover:text-t1" @click="closeCred">
            <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M18 6L6 18M6 6l12 12"/></svg>
          </button>
        </div>
        <div class="px-6 py-5">
          <label class="block text-[13px] text-t2 mb-1.5">API Key</label>
          <input v-model="credApiKey" type="password" autocomplete="new-password"
            class="input" placeholder="填写新的 API Key" />
          <p class="text-xs text-t3 mt-1.5">
            {{ credTarget.outbound_credential_configured
              ? '该服务已配置 API Key（值不回显）。填写新值可覆盖；留空保存会清除。'
              : '尚未配置。保存后调用服务时自动放入 apikey 请求头。' }}
          </p>
        </div>
        <div class="flex justify-end gap-2 px-6 py-4 bg-canvas border-t border-line">
          <button class="btn btn-ghost" :disabled="credBusy" @click="closeCred">取消</button>
          <button
            class="btn"
            :class="(!credApiKey.trim() && credTarget.outbound_credential_configured) ? 'bg-down text-white hover:bg-down/90' : 'btn-primary'"
            :disabled="credBusy || (!credApiKey.trim() && !credTarget.outbound_credential_configured)"
            @click="saveCred">
            {{ credBusy ? '保存中…' : (credApiKey.trim() ? '保存 API Key' : '清空 API Key') }}
          </button>
        </div>
      </div>
    </div>
  </div>
  <HiAgentRunDialog v-if="runService" :key="runService.service_id" :service="runService" @close="runService = null" />
</template>
