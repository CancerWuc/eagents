<script setup>
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { api } from '../api.js'
import { config } from '../config.js'
import { slugValidationError } from '../form-rules.js'
import Pagination from '../components/Pagination.vue'
import { useServerPagination, watchPagination } from '../pagination.js'

const emit = defineEmits(['open'])
defineProps({ canManage: { type: Boolean, default: false } })
const masters = ref([])
const details = ref({})   // master_id -> getMaster 响应（绑定/提示词/调用量）
const liveness = ref({})  // master_id -> 'checking' | 'live' | 'pending'
const loading = ref(true)
const err = ref('')
const copied = ref('')

const search = ref('')
const tile = ref('all')          // all | live | pending | unbound
const enabledFilter = ref('all') // all | on | off
const stats = ref({})
const teamsPager = useServerPagination(masters, () => '/api/v1/agents', {
  filters: () => ({ q: search.value.trim(), tile: tile.value, enabled: enabledFilter.value }),
  onResult: result => {
    stats.value = result.stats || {}
    liveness.value = Object.fromEntries(result.items.map(m => [m.master_id, m.liveness]))
    details.value = Object.fromEntries(result.items.map(m => [m.master_id, {
      bindings: m.bindings || [], kb_bindings: m.kb_bindings || [],
      prompts: [{ version_no: m.latest_prompt_version_no }], prompt_count: m.prompt_count,
      runs_stat: { total: m.runs_total, ok: m.runs_ok, failed: m.runs_failed },
    }]))
  },
})
watchPagination(teamsPager, [search], 300)
watchPagination(teamsPager, [tile, enabledFilter])

// 新增表单
const showForm = ref(false)
const busy = ref(false)
const form = ref({ slug: '', displayName: '', description: '', maxIters: 8 })

const a2aBase = config.apiBase || window.location.origin
const endpoint = (m) => `${a2aBase}/a2a/${m.slug}/`
const maxItersOf = (m) => {
  try { return JSON.parse(m.react_config_json || '{}').max_iters ?? 8 } catch { return 8 }
}
const modelName = (m) => m.model_config?.model || '全局默认'
const fmtDate = (t) => (t ? String(t).replace('T', ' ').slice(0, 16) : '—')
const num = (v) => Number(v ?? 0) || 0

const bindingsOf = (id) => details.value[id]?.bindings || []
// 知识库绑定走独立接口，不在 /agents/{id} 的 bindings 里；此前列表与详情
// 都没取，导致知识库页显示「被引用」而 Agent 侧查无此事
const kbBindingsOf = (id) => details.value[id]?.kb_bindings || []
const promptVerOf = (id) => details.value[id]?.prompts?.[0]?.version_no ?? null
const runsOf = (id) => details.value[id]?.runs_stat || {}

async function load() {
  err.value = ''
  try {
    await teamsPager.load()
    loading.value = false
  } catch (e) {
    err.value = e.message
    loading.value = false
  }
}

onMounted(() => load())

// --- 统计与筛选 -----------------------------------------------------------
const filtered = computed(() => masters.value)

const dirty = computed(() =>
  !!search.value.trim() || tile.value !== 'all' || enabledFilter.value !== 'all')

function reset() {
  search.value = ''
  tile.value = 'all'
  enabledFilter.value = 'all'
}
function pickTile(k) { tile.value = tile.value === k ? 'all' : k }

// --- 新增 -----------------------------------------------------------------
const slugError = computed(() => {
  const v = form.value.slug.trim()
  if (!v) return ''
  const error = slugValidationError(v)
  if (error) return error
  if (masters.value.some(m => m.slug === v)) return '该标识已被占用'
  return ''
})
const itersError = computed(() => {
  const n = Number(form.value.maxIters)
  return Number.isInteger(n) && n >= 1 && n <= 50 ? '' : '需为 1–50 的整数'
})
const formValid = computed(() =>
  !!form.value.slug.trim() && !!form.value.displayName.trim()
  && !slugError.value && !itersError.value)

function openForm() {
  form.value = { slug: '', displayName: '', description: '', maxIters: 8 }
  showForm.value = true
}
function closeForm() {
  if (busy.value) return
  showForm.value = false
}
async function submit() {
  if (!formValid.value) return
  busy.value = true
  try {
    await api.createMaster({
      slug: form.value.slug.trim(),
      displayName: form.value.displayName.trim(),
      description: form.value.description.trim() || undefined,
      maxIters: Number(form.value.maxIters),
    })
    showForm.value = false
    await load()
  } catch { /* toast 已展示 */ } finally {
    busy.value = false
  }
}

// --- 删除（级联，不可恢复）-------------------------------------------------
const delTarget = ref(null)
const delConfirm = ref('')
const deleting = ref(false)
const delResult = ref(null)

function askDelete(m) {
  delTarget.value = m
  delConfirm.value = ''
  delResult.value = null
}
function closeDelete() {
  if (deleting.value) return
  delTarget.value = null
  delResult.value = null
}
const delReady = computed(() =>
  !!delTarget.value && delConfirm.value.trim() === delTarget.value.slug)

async function confirmDelete() {
  if (!delReady.value) return
  deleting.value = true
  try {
    delResult.value = await api.deleteMaster(
      delTarget.value.master_id, delTarget.value.slug)
    await load()
  } catch { /* toast 已展示 */ } finally {
    deleting.value = false
  }
}

const cascadeRows = computed(() => {
  const c = delResult.value?.cascade || {}
  return Object.entries(c).sort((a, b) => b[1] - a[1])
})

async function copyEndpoint(m) {
  try {
    await navigator.clipboard.writeText(endpoint(m))
    copied.value = m.master_id
    setTimeout(() => { if (copied.value === m.master_id) copied.value = '' }, 1500)
  } catch { /* 非安全上下文不支持剪贴板 */ }
}

function onKey(e) {
  if (e.key !== 'Escape') return
  if (delTarget.value) closeDelete()
  else if (showForm.value) closeForm()
}
watch([showForm, delTarget], ([a, b]) => {
  document.body.style.overflow = (a || b) ? 'hidden' : ''
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
        <h1 class="text-xl font-semibold">Agent Team</h1>
        <p class="text-t2 mt-1">Team 负责理解用户意图，再决定调用哪个已绑定的服务。每个 Team 有自己独立的调用地址。</p>
      </div>
      <button v-if="canManage" class="btn btn-primary" @click="openForm">
        <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M12 5v14M5 12h14"/></svg>新建 Agent Team
      </button>
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
          <span class="text-sm text-t2">Agent Team</span>
          <div class="w-10 h-10 rounded-[10px] bg-brand-light text-brand flex items-center justify-center">
            <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87"/><path d="M16 3.13a4 4 0 010 7.75"/></svg>
          </div>
        </div>
        <div class="text-[34px] font-bold mt-2.5 leading-none">{{ stats.total }}</div>
        <div class="mt-3 text-[13px] text-t3">已启用 {{ stats.enabled }} · 已停用 {{ stats.disabled }}</div>
      </button>

      <button
        :class="['bg-white rounded-xl shadow-sm p-5 text-left border transition-colors',
                 tile === 'live' ? 'border-brand' : 'border-transparent hover:border-line']"
        @click="pickTile('live')">
        <div class="flex justify-between items-start">
          <span class="text-sm text-t2">调用地址已就绪</span>
          <div class="w-10 h-10 rounded-[10px] bg-up-light text-up flex items-center justify-center">
            <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M9 12l2 2 4-4"/><circle cx="12" cy="12" r="9"/></svg>
          </div>
        </div>
        <div class="text-[34px] font-bold mt-2.5 leading-none">{{ stats.live }}</div>
        <div :class="['mt-3 text-[13px]', stats.pending ? 'text-warn' : 'text-t3']">
          {{ stats.pending ? `${stats.pending} 个探测不可达` : '全部可达' }}
        </div>
      </button>

      <button
        :class="['bg-white rounded-xl shadow-sm p-5 text-left border transition-colors',
                 tile === 'unbound' ? 'border-brand' : 'border-transparent hover:border-line']"
        @click="pickTile('unbound')">
        <div class="flex justify-between items-start">
          <span class="text-sm text-t2">绑定服务</span>
          <div class="w-10 h-10 rounded-[10px] bg-brand-light text-brand flex items-center justify-center">
            <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M4 7h16M4 12h16M4 17h16"/><circle cx="8" cy="7" r="1.6"/><circle cx="13" cy="12" r="1.6"/></svg>
          </div>
        </div>
        <div class="text-[34px] font-bold mt-2.5 leading-none">{{ stats.bindings }}
          <span class="text-base text-t3 font-normal"> / {{ stats.services }} 可用</span>
        </div>
        <div :class="['mt-3 text-[13px]', stats.unbound ? 'text-warn' : 'text-t3']">
          {{ stats.unbound ? `${stats.unbound} 个 Team 未绑定服务` : '全部已绑定' }}
        </div>
      </button>

      <!-- 信息卡：调用量不构成筛选条件，故无 hover / 选中态 -->
      <div class="bg-white rounded-xl shadow-sm p-5 border border-transparent">
        <div class="flex justify-between items-start">
          <span class="text-sm text-t2">累计调用次数</span>
          <div class="w-10 h-10 rounded-[10px] bg-warn-light text-warn flex items-center justify-center">
            <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M21 12a9 9 0 11-2.64-6.36L21 8"/><path d="M21 3v5h-5"/></svg>
          </div>
        </div>
        <div class="text-[34px] font-bold mt-2.5 leading-none">{{ stats.runs }}</div>
        <div class="mt-3 text-[13px] text-t3">成功 {{ stats.ok }} · 失败 {{ stats.failed }}</div>
      </div>
    </div>

    <!-- 列表 -->
    <div class="card !p-0">
      <div class="flex items-center gap-3 px-5 py-4 border-b border-line">
        <div class="flex items-center gap-2 border border-line rounded-lg px-3 bg-white w-72 focus-within:border-brand transition-colors">
          <svg class="w-4 h-4 text-t3 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>
          <input v-model="search" placeholder="搜索名称 / 标识 / 描述" class="h-9 flex-1 outline-none text-sm bg-transparent" />
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
          共 <span class="text-t1 font-medium">{{ teamsPager.total }}</span> 个 Team
          <span v-if="dirty">（已筛选，总数 {{ stats.total }}）</span>
        </div>
      </div>

      <div class="overflow-x-auto">
        <table class="tbl">
          <thead>
            <tr>
              <th class="!pl-5">Agent Team</th>
              <th class="w-44">绑定服务</th>
              <th class="w-40">知识库</th>
              <th class="w-32">提示词</th>
              <th class="w-36">模型 / 最大轮数</th>
              <th class="w-32">调用量</th>
              <th class="w-24">启用</th>
              <th class="w-28 !pr-5"></th>
            </tr>
          </thead>
          <tbody>
            <template v-if="loading">
              <tr v-for="n in 3" :key="`sk-${n}`">
                <td class="!pl-5"><div class="h-4 w-40 rounded bg-canvas animate-pulse"></div><div class="h-3 w-64 rounded bg-canvas animate-pulse mt-2"></div></td>
                <td><div class="h-4 w-20 rounded bg-canvas animate-pulse"></div></td>
                <td><div class="h-5 w-16 rounded bg-canvas animate-pulse"></div></td>
                <td><div class="h-4 w-20 rounded bg-canvas animate-pulse"></div></td>
                <td><div class="h-4 w-12 rounded bg-canvas animate-pulse"></div></td>
                <td><div class="h-5 w-14 rounded bg-canvas animate-pulse"></div></td>
                <td class="!pr-5"><div class="h-5 w-16 rounded bg-canvas animate-pulse"></div></td>
              </tr>
            </template>

            <template v-else>
            <tr v-for="m in filtered" :key="m.master_id" class="align-top group">
              <!-- 名称 / slug / 描述 / A2A 端点 -->
              <td class="!pl-5">
                <div class="flex items-center gap-2">
                  <span class="font-semibold text-t1">{{ m.display_name || m.slug }}</span>
                  <span class="mono text-[12px] bg-canvas text-t2 px-1.5 py-0.5 rounded">{{ m.slug }}</span>
                </div>
                <div v-if="m.description" class="text-[13px] text-t3 mt-1 truncate max-w-[24rem]" :title="m.description">
                  {{ m.description }}
                </div>
                <div class="flex items-center gap-1.5 mt-1.5">
                  <svg class="w-3.5 h-3.5 text-t3 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M10 13a5 5 0 007.5.5l3-3a5 5 0 00-7-7l-1.7 1.7"/><path d="M14 11a5 5 0 00-7.5-.5l-3 3a5 5 0 007 7l1.7-1.7"/></svg>
                  <span class="mono text-[12px] truncate max-w-[22rem]" :title="endpoint(m)">{{ endpoint(m) }}</span>
                  <button class="text-t3 hover:text-brand opacity-0 group-hover:opacity-100 transition-opacity shrink-0"
                          :title="copied === m.master_id ? '已复制' : '复制 A2A 端点'"
                          @click="copyEndpoint(m)">
                    <svg v-if="copied === m.master_id" class="w-3.5 h-3.5 text-up" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6L9 17l-5-5"/></svg>
                    <svg v-else class="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 012-2h10"/></svg>
                  </button>
                </div>
                <div v-if="liveness[m.master_id] === 'pending'"
                     class="flex items-start gap-1.5 mt-1 text-[12px] text-warn"
                     title="Agent Card 探测不可达。多副本部署时其他副本新建的 Team 本副本可能尚未挂载；单副本下出现说明挂载失败，请查后端日志 eagent.a2a.mounts">
                  <svg class="w-3.5 h-3.5 shrink-0 mt-px" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9L2.6 17a2 2 0 001.7 3h15.4a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z"/></svg>
                  端点探测不可达
                </div>
              </td>

              <td>
                <div class="font-semibold">{{ bindingsOf(m.master_id).length }}
                  <span class="text-t3 font-normal text-[13px]">个</span>
                </div>
                <div class="text-xs text-t3 mt-1 truncate max-w-[10rem]"
                     :title="bindingsOf(m.master_id).map(b => b.tool_name).join(' / ')">
                  {{ bindingsOf(m.master_id).map(b => b.tool_name).join(' / ') || '未绑定服务' }}
                </div>
              </td>

              <td>
                <div class="font-semibold">{{ kbBindingsOf(m.master_id).length }}
                  <span class="text-t3 font-normal text-[13px]">个</span>
                </div>
                <div class="text-xs text-t3 mt-1 truncate max-w-[9rem]"
                     :title="kbBindingsOf(m.master_id).map(b => b.display_name).join(' / ')">
                  {{ kbBindingsOf(m.master_id).map(b => b.display_name).join(' / ') || '未绑定知识库' }}
                </div>
              </td>

              <td>
                <span v-if="promptVerOf(m.master_id) != null" class="badge badge-ok">v{{ promptVerOf(m.master_id) }}</span>
                <span v-else class="badge badge-gray">还没有提示词版本</span>
                <div class="text-xs text-t3 mt-1">
                  {{ details[m.master_id]?.prompt_count ?? 0 }} 个版本
                </div>
              </td>

              <td>
                <div class="text-[13px] truncate max-w-[8rem]" :title="modelName(m)">{{ modelName(m) }}</div>
                <div class="text-xs text-t3 mt-1">最多 {{ maxItersOf(m) }} 轮</div>
              </td>

              <td class="whitespace-nowrap">
                <div class="font-semibold">{{ num(runsOf(m.master_id).total) }}</div>
                <div class="text-xs text-t3 mt-1">
                  成功 {{ num(runsOf(m.master_id).ok) }} · 失败 {{ num(runsOf(m.master_id).failed) }}
                </div>
              </td>

              <!-- 后端未提供 Team 启停接口，此处只读展示 -->
              <td class="whitespace-nowrap">
                <span :class="m.enabled ? 'badge badge-ok' : 'badge badge-gray'">
                  {{ m.enabled ? '已启用' : '已停用' }}
                </span>
                <div class="text-xs text-t3 mt-1">{{ fmtDate(m.created_at) }}</div>
              </td>

              <td class="!pr-5 whitespace-nowrap">
                <button class="btn btn-text" @click="emit('open', m.master_id)">详情</button>
                <button v-if="canManage" class="!h-7 px-1 align-middle text-t3 hover:text-brand opacity-0 group-hover:opacity-100 transition-opacity"
                        title="删除该 Agent Team（级联，不可恢复）"
                        @click="askDelete(m)">
                  <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6"/></svg>
                </button>
              </td>
            </tr>
            </template>

            <!-- 空态：首次无数据 vs 筛选无结果，给不同出口 -->
            <tr v-if="!loading && !filtered.length">
              <td colspan="7" class="!py-16">
                <div class="flex flex-col items-center text-center">
                  <div class="w-12 h-12 rounded-xl bg-canvas text-t3 flex items-center justify-center mb-3">
                    <svg class="w-6 h-6" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87"/></svg>
                  </div>
                  <template v-if="stats.total">
                    <div class="text-t1 font-medium">没有匹配的 Agent Team</div>
                    <div class="text-t3 mt-1">试试调整搜索词或筛选条件</div>
                    <button class="btn btn-ghost btn-sm mt-4" @click="reset">重置筛选</button>
                  </template>
                  <template v-else>
                    <div class="text-t1 font-medium">还没有 Agent Team</div>
                    <div class="text-t3 mt-1">建好后会得到一个独立的调用地址，可以把已注册的服务与 HIA 智能体绑给它</div>
                    <button v-if="canManage" class="btn btn-primary btn-sm mt-4" @click="openForm">新建 Agent Team</button>
                  </template>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
        <Pagination :pager="teamsPager" />
      </div>
    </div>

    <!-- 新增弹窗 -->
    <div v-if="showForm" class="fixed inset-0 bg-black/30 backdrop-blur-[1px] flex items-center justify-center z-50 p-4" @click.self="closeForm">
      <div class="bg-white rounded-xl w-[540px] shadow-xl overflow-hidden">
        <div class="flex items-center justify-between px-6 py-4 border-b border-line">
          <h3 class="font-semibold text-base">新建 Agent Team</h3>
          <button class="text-t3 hover:text-t1" @click="closeForm">
            <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M18 6L6 18M6 6l12 12"/></svg>
          </button>
        </div>

        <div class="px-6 py-5">
          <div class="mb-4">
            <label class="block text-[13px] text-t2 mb-1.5">Team 标识 <span class="text-brand">*</span></label>
            <input v-model="form.slug" class="input" :class="slugError && '!border-brand'" placeholder="finance-desk" @keydown.enter="submit" />
            <p :class="['text-xs mt-1.5', slugError ? 'text-brand' : 'text-t3']">
              {{ slugError || `决定 A2A 调用地址：${a2aBase}/a2a/${form.slug.trim() || '<slug>'}/` }}
            </p>
          </div>
          <div class="mb-4">
            <label class="block text-[13px] text-t2 mb-1.5">Team 名称 <span class="text-brand">*</span></label>
            <input v-model="form.displayName" class="input" placeholder="财务台 Team" @keydown.enter="submit" />
            <p class="text-xs text-t3 mt-1.5">管理台和对外名片上显示的名称</p>
          </div>
          <div class="mb-4">
            <label class="block text-[13px] text-t2 mb-1.5">Team 描述</label>
            <textarea v-model="form.description" rows="2" class="input resize-none" placeholder="按用户意图调度财务域的已绑定服务"></textarea>
            <p class="text-xs text-t3 mt-1.5">会写进这个 Team 的名片，让调用方知道它管什么</p>
          </div>
          <div>
            <label class="block text-[13px] text-t2 mb-1.5">最大推理轮数（单次回复内最多循环几轮，建议 3–10） <span class="text-brand">*</span></label>
            <input v-model="form.maxIters" type="number" min="1" max="50" class="input !w-32" :class="itersError && '!border-brand'" @keydown.enter="submit" />
            <p :class="['text-xs mt-1.5', itersError ? 'text-brand' : 'text-t3']">
              {{ itersError || 'ReAct 循环单次运行的工具调用上限，1–50，之后可在详情页调整' }}
            </p>
          </div>
        </div>

        <div class="flex justify-end gap-2 px-6 py-4 bg-canvas border-t border-line">
          <button class="btn btn-ghost" :disabled="busy" @click="closeForm">取消</button>
          <button class="btn btn-primary" :disabled="busy || !formValid" @click="submit">
            <svg v-if="busy" class="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M21 12a9 9 0 11-2.64-6.36"/></svg>
            {{ busy ? '创建中…' : '创建' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 删除确认 / 结果 -->
    <div v-if="delTarget" class="fixed inset-0 bg-black/30 backdrop-blur-[1px] flex items-center justify-center z-50 p-4" @click.self="closeDelete">
      <div class="bg-white rounded-xl w-[540px] shadow-xl overflow-hidden">
        <div class="flex items-center justify-between px-6 py-4 border-b border-line">
          <h3 class="font-semibold text-base">{{ delResult ? '已删除' : '删除 Agent Team' }}</h3>
          <button class="text-t3 hover:text-t1" @click="closeDelete">
            <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M18 6L6 18M6 6l12 12"/></svg>
          </button>
        </div>

        <div v-if="!delResult" class="px-6 py-5">
          <div class="flex gap-3 p-3 rounded-lg bg-brand-lighter text-brand mb-4">
            <svg class="w-5 h-5 shrink-0 mt-px" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9L2.6 17a2 2 0 001.7 3h15.4a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z"/></svg>
            <div class="text-[13px] leading-6">
              将级联删除
              <span class="font-semibold">{{ delTarget.display_name || delTarget.slug }}</span>
              及其全部从属数据：服务绑定、提示词版本、运行记录、调用事件、评测集与标注、
              优化任务与候选、性能测试数据集与样本、KB 绑定、standalone 导出登记。
              <span class="font-semibold">此操作不可恢复。</span>
            </div>
          </div>
          <p class="text-[13px] text-t2 mb-2">
            请输入该 Team 的 slug <span class="mono bg-canvas px-1.5 py-0.5 rounded">{{ delTarget.slug }}</span> 以确认：
          </p>
          <input v-model="delConfirm" class="input" :placeholder="delTarget.slug" @keydown.enter="confirmDelete" />
          <p class="text-xs text-t3 mt-2">
            /a2a/{{ delTarget.slug }} 端点会同步摘除，立即返回 404。
          </p>
        </div>

        <div v-else class="px-6 py-5">
          <p class="text-[13px] text-t2 mb-3">
            <span class="mono">{{ delResult.slug }}</span> 已删除，级联清理如下：
          </p>
          <div class="rounded-lg border border-line divide-y divide-line max-h-64 overflow-y-auto">
            <div v-for="[table, n] in cascadeRows" :key="table"
                 class="flex items-center justify-between px-3 py-2">
              <span class="mono text-[12px]">{{ table }}</span>
              <span class="text-[13px] text-t2">{{ n }} 行</span>
            </div>
          </div>
        </div>

        <div class="flex justify-end gap-2 px-6 py-4 bg-canvas border-t border-line">
          <template v-if="!delResult">
            <button class="btn btn-ghost" :disabled="deleting" @click="closeDelete">取消</button>
            <button class="btn btn-primary" :disabled="deleting || !delReady" @click="confirmDelete">
              <svg v-if="deleting" class="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M21 12a9 9 0 11-2.64-6.36"/></svg>
              {{ deleting ? '删除中…' : '确认删除' }}
            </button>
          </template>
          <button v-else class="btn btn-primary" @click="closeDelete">完成</button>
        </div>
      </div>
    </div>
  </div>
</template>
