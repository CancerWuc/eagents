<script setup>
// 知识库 Tab（from_llm_wiki 01 §5 / T19-T21）：
// 列表 + 详情（概览/原始文档/整理方案/生成的页面/规则设置/检索测试/质量问题/编译记录/绑定 Agent）
// 用户可见文案一律用业务白话，枚举原值保留在 title 提示中供排障
// 整理方案卡片：placement + changes 勾选（依赖闭包） + preview diff + 确认/驳回（D8 文案）
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from '../api.js'
import { promptText, confirmAction } from '../ui.js'
import { notify } from '../notify.js'

import Pagination from '../components/Pagination.vue'
import { useServerPagination } from '../pagination.js'

defineProps({ canManage: { type: Boolean, default: false } })

const kbs = ref([])
const bindingKbs = ref([])
const loading = ref(false)
const showForm = ref(false)
const form = ref({ slug: '', display_name: '', description: '', classification: 'INTERNAL', schema_text: '', purpose_text: '' })

const detail = ref(null)          // 当前 KB
const tab = ref('overview')
const sources = ref([])
const pages = ref([])
const revs = ref([])
const plans = ref([])
const reviews = ref([])
const tasks = ref([])
const bindings = ref([])
const masters = ref([])
const searchQ = ref('')
const searchResult = ref(null)
const searching = ref(false)
const uploadBusy = ref(false)
const rejectPlanId = ref(null)

const kbsPager = useServerPagination(kbs, () => '/api/v1/knowledge-bases')
const kbUrl = suffix => `/api/v1/knowledge-bases/${detail.value?.kb_id}/${suffix}`
const sourcesPager = useServerPagination(sources, () => kbUrl('sources'))
const pagesPager = useServerPagination(pages, () => kbUrl('pages'))
const revsPager = useServerPagination(revs, () => kbUrl('revs'))
const plansPager = useServerPagination(plans, () => kbUrl('ingest-plans'))
const reviewsPager = useServerPagination(reviews, () => kbUrl('reviews') + '?status=OPEN')
const tasksPager = useServerPagination(tasks, () => kbUrl('tasks'))
const invalidateDetails = () => [sourcesPager, pagesPager, revsPager, plansPager, reviewsPager, tasksPager].forEach(p => p.invalidate())
let pollTimer = null
let detailRevision = 0

async function load() {
  loading.value = true
  try { await kbsPager.load() } finally { loading.value = false }
}

async function submit() {
  if (!form.value.slug || !form.value.display_name) return
  await api.createKb(form.value)
  showForm.value = false
  form.value = { slug: '', display_name: '', description: '', classification: 'INTERNAL', schema_text: '', purpose_text: '' }
  await load()
}

// ---- 删除知识库（硬删，不可恢复；409 时二次确认走 force）----
const deleting = ref('')
async function removeKb(kb) {
  if (!await confirmAction(`删除知识库「${kb.display_name}」？将物理删除它的全部原始文档、生成页面、历史版本、整理方案与绑定关系，不可恢复。`, { okText: '删除', danger: true })) return
  deleting.value = kb.kb_id
  try {
    let out
    try {
      out = await api.deleteKb(kb.kb_id)
      notify({ code: 'KB_DELETED', message: `已删除知识库 ${out.slug}` })
    } catch (err) {
      if (err.code !== 'CONFLICT') throw err
      if (!await confirmAction(`${err.message} 仍要强制删除吗？绑定会被一并移除，进行中的编译任务会被丢弃。`, { okText: '强制删除', danger: true })) return
      out = await api.deleteKb(kb.kb_id, true)
      notify({ code: 'KB_DELETED', message: `已强制删除 ${out.slug}（解绑 ${out.unbound} 处）` })
    }
    if (out.storage_error) notify({ code: 'STORAGE_WARN', message: `数据已删除，但存储目录清理失败：${out.storage_error}` })
    if (detail.value?.kb_id === kb.kb_id) back()
    else await load()
  } finally { deleting.value = '' }
}

// 状态中文标签（原始枚举值保留在 title 提示里，便于对接口排障）
const KB_STATUS_TEXT = { CREATED: '新建·无内容', PARSING: '编译中', READY: '可用', STALE: '待重新编译', FAILED: '编译失败', ARCHIVED: '已归档' }
const SRC_STATUS_TEXT = { PENDING: '待整理', PLANNING: '整理中', INGESTING: '编译中', DONE: '已入库', FAILED: '失败', DELETED: '已删除' }
const PLAN_STATUS_TEXT = { DRAFT: '待你确认', CONFIRMED: '已确认·待编译', APPLIED: '已生效', APPLIED_WITH_SKIPS: '部分生效', REJECTED: '已驳回', SUPERSEDED: '已被新方案替代' }
const TASK_STATUS_TEXT = { PENDING: '排队中', RUNNING: '执行中', DONE: '已完成', FAILED: '失败', CANCELED: '已取消' }
const TASK_KIND_TEXT = { INGEST: '增量编译', REBUILD: '重建索引', EMBED: '补切块' }
const REV_STATUS_TEXT = { BUILDING: '构建中', READY: '已发布', FAILED: '失败' }
const REVIEW_TYPE_TEXT = { contradiction: '内容矛盾', duplicate: '内容重复', missing_page: '断链', suggestion: '优化建议' }
const ACTION_TEXT = { CREATE_PAGE: '新建页面', UPDATE_PAGE: '更新已有页面', RELINK: '修改页面间链接', UPDATE_INDEX_LOG: '更新索引与变更日志' }
function t(map, k) { return map[k] || k }

const statusBadge = { READY: 'badge badge-ok', CREATED: 'badge badge-gray', PARSING: 'badge badge-warn', STALE: 'badge badge-warn', FAILED: 'badge badge-err', ARCHIVED: 'badge badge-gray' }

async function open(kb) {
  const revision = ++detailRevision
  invalidateDetails()
  const result = await api.getKb(kb.kb_id)
  if (revision !== detailRevision) return
  detail.value = result
  tab.value = 'overview'
  await refreshAll()
  startPolling()
}
function back() { detailRevision++; stopPolling(); invalidateDetails(); detail.value = null; load() }

async function refreshAll() {
  const id = detail.value?.kb_id
  if (!id) return
  await Promise.all([sourcesPager.load(), pagesPager.load(), revsPager.load(),
    plansPager.load(), reviewsPager.load(), tasksPager.load()])
  const refreshed = await api.getKb(id)
  if (detail.value?.kb_id === id) detail.value = refreshed
}

// 5s 轮询：有活跃任务或待确认规划时刷新（B4）
function startPolling() {
  stopPolling()
  pollTimer = setInterval(async () => {
    if (!detail.value) return stopPolling()
    // 源处于 PENDING/PLANNING 说明规划正在路上（30s 防抖窗口内既无任务
    // 也无 DRAFT，漏掉这一条会导致规划生成后页面不自动刷新）
    if (!detail.value.active_work) return
    try { await refreshAll() } catch { /* 轮询失败静默，下一轮重试 */ }
  }, 5000)
}
function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null } }
onBeforeUnmount(stopPolling)
onMounted(load)

// ---- sources ----
// 上传区（T19 可用性修复）：原先是裸 <input type="file">，浏览器默认样式
// 极不显眼，用户找不到入口。改为显式按钮 + 拖拽区 + 空态引导。
const ACCEPT = '.md,.markdown,.txt,.json,.csv,.pdf'
const MAX_UPLOAD_MB = 20
const fileInput = ref(null)
const dragging = ref(false)
const uploadProgress = ref('')

function pickFile() { fileInput.value?.click() }

async function uploadFiles(files) {
  const list = [...(files || [])].filter(Boolean)
  if (!list.length) return
  const oversize = list.filter(f => f.size > MAX_UPLOAD_MB * 1024 * 1024)
  if (oversize.length) {
    notify({ code: 'TOO_LARGE', message: `超过 ${MAX_UPLOAD_MB}MB 上限，已跳过：${oversize.map(f => f.name).join('、')}` })
  }
  const todo = list.filter(f => f.size <= MAX_UPLOAD_MB * 1024 * 1024)
  if (!todo.length) return

  uploadBusy.value = true
  let ok = 0, dup = 0
  try {
    for (const [i, file] of todo.entries()) {
      uploadProgress.value = todo.length > 1 ? `${i + 1}/${todo.length}：${file.name}` : file.name
      try {
        const out = await api.uploadKbSource(detail.value.kb_id, file)
        out.duplicate_of ? dup++ : ok++
      } catch { /* 单份失败不中断其余；错误已由 api 层全局通知 */ }
    }
    const parts = []
    if (ok) parts.push(`成功 ${ok} 份，正在生成整理方案`)
    if (dup) parts.push(`${dup} 份内容重复已跳过`)
    if (parts.length) notify({ code: ok ? 'UPLOADED' : 'DUPLICATE', message: parts.join('；') })
    await refreshAll()
    if (ok) tab.value = 'plans'   // 上传成功直接带到待确认的下一步
  } finally {
    uploadBusy.value = false
    uploadProgress.value = ''
  }
}

async function onUpload(e) {
  const files = e.target.files
  await uploadFiles(files)
  e.target.value = ''
}

function onDrop(e) {
  dragging.value = false
  if (!uploadBusy.value) uploadFiles(e.dataTransfer?.files)
}
async function removeSource(s) {
  if (!await confirmAction(`删除原始文档「${s.file_name}」？这只是标记删除，已经由它生成的页面不会消失、仍可被检索到；要真正清理需重新整理并编译。`, { okText: '删除', danger: true })) return
  await api.deleteKbSource(detail.value.kb_id, s.source_id)
  await refreshAll()
}
async function replan(s) {
  await api.replanKbSource(detail.value.kb_id, s.source_id)
  notify({ code: 'PLANNING', message: '已重新整理，稍后在「整理方案」查看' })
  setTimeout(refreshAll, 1500)
}

// ---- 编译动作 ----
async function trigger(kind) {
  const fn = { INGEST: api.kbIngest, REBUILD: api.kbRebuild, EMBED: api.kbEmbed }[kind]
  const out = await fn(detail.value.kb_id)
  notify({ code: 'TASK_QUEUED', message: out.message })
  await refreshAll()
}
async function cancelTask(t) {
  await api.cancelKbTask(detail.value.kb_id, t.task_id)
  await refreshAll()
}

// ---- Schema/Purpose 编辑 ----
const schemaEdit = ref(false)
const schemaDraft = ref({ schema_text: '', purpose_text: '' })
function editSchema() {
  schemaDraft.value = { schema_text: detail.value.schema_text || '', purpose_text: detail.value.purpose_text || '' }
  schemaEdit.value = true
}
async function saveSchema() {
  await api.updateKb(detail.value.kb_id, schemaDraft.value)
  schemaEdit.value = false
  await refreshAll()
}

// ---- 整理方案（T21）----
const draftPlans = computed(() => plans.value.filter(p => p.status === 'DRAFT'))
const selectedActions = ref({})   // plan_id → Set(action_id)（默认全勾）
function isSelected(plan, id) { return (selectedActions.value[plan.plan_id] || plan.proposal.changes.map(c => c.action_id)).includes(id) }
function toggle(plan, id) {
  const all = plan.proposal.changes.map(c => c.action_id)
  const cur = new Set(selectedActions.value[plan.plan_id] || all)
  cur.has(id) ? cur.delete(id) : cur.add(id)
  selectedActions.value[plan.plan_id] = [...cur]
}
const CONFIRM_NOTICE = '确认后会按勾选的动作整理页面（新建/更新/改链接/更新索引）。页面正文由大模型生成，生成后自动质检；发现内容矛盾、重复或断链会进入「质量问题」等你处理。'
async function confirmPlan(plan) {
  const ids = (selectedActions.value[plan.plan_id] || plan.proposal.changes.map(c => c.action_id))
  if (!await confirmAction(CONFIRM_NOTICE, { okText: '确认并编译' })) return
  const out = await api.confirmKbPlan(detail.value.kb_id, plan.plan_id, { selected_action_ids: ids, note: '页面确认' })
  notify({ code: 'CONFIRMED', message: `已确认，编译任务已排队（${out.task_id}）${out.auto_selected?.length ? '；已自动补勾被依赖的动作' : ''}，进度看「编译记录」` })
  await refreshAll()
}
async function rejectPlan(plan) {
  const reason = await promptText('驳回这份整理方案：请写明原因（必填）。原因会作为参考喂给后续的整理，避免重复给出同样的方案。')
  if (!reason) return
  await api.rejectKbPlan(detail.value.kb_id, plan.plan_id, reason)
  await refreshAll()
}

// ---- 检索预览 ----
async function doSearch() {
  if (!searchQ.value) return
  searching.value = true
  try { searchResult.value = await api.kbPreviewSearch(detail.value.kb_id, searchQ.value) }
  finally { searching.value = false }
}

// ---- Review ----
async function resolveReview(r, status) {
  await api.resolveKbReview(detail.value.kb_id, r.review_id, status)
  await refreshAll()
}

// ---- 绑定 ----
async function loadBindings() {
  bindingKbs.value = (await api.listKbs()).knowledge_bases
  masters.value = (await api.listAgents()).composite
  bindings.value = []
}
const bindMasterId = ref('')
async function pickMaster() {
  if (!bindMasterId.value) return
  bindings.value = (await api.getKbBindings(bindMasterId.value)).bindings
}
function boundKb(kbId) { return bindings.value.some(b => b.kb_id === kbId) }
async function toggleBind(kb) {
  const next = bindings.value.filter(b => b.kb_id !== kb.kb_id)
  if (!boundKb(kb.kb_id)) next.push({ kb_id: kb.kb_id, enabled: true })
  const out = await api.putKbBindings(bindMasterId.value, next)
  bindings.value = out.bindings
  notify({ code: 'BINDINGS_SET', message: `绑定已更新，该 Agent 当前可检索 ${out.bindings.length} 个知识库` })
}

function fmtTime(t) { return (t || '').replace('T', ' ').slice(0, 19) }
</script>

<template>
  <!-- ============ 列表 ============ -->
  <div v-if="!detail">
    <div class="flex items-center justify-between mb-5">
      <div>
        <h2 class="text-xl font-semibold">知识库</h2>
        <p class="text-sm text-t3 mt-1">上传文档 → 机器给出整理方案 → 你确认 → 编译成页面 → 绑给 Agent 检索使用</p>
      </div>
      <button v-if="canManage" class="btn btn-primary" @click="showForm = true">+ 新建知识库</button>
    </div>
    <div class="card !p-0 overflow-x-auto">
      <table class="tbl">
        <thead><tr>
          <th>名称 / 标识</th><th>状态</th><th>原始文档</th><th>生成页面</th><th>当前版本</th><th>被几个 Agent 使用</th><th>更新时间</th><th></th>
        </tr></thead>
        <tbody>
          <tr v-if="!kbs.length && !loading"><td colspan="8" class="text-center text-t3 py-10">暂无知识库</td></tr>
          <tr v-for="kb in kbs" :key="kb.kb_id" class="cursor-pointer" @click="open(kb)">
            <td>
              <div class="font-medium">{{ kb.display_name }}</div>
              <div class="text-xs text-t3 font-mono">{{ kb.slug }}</div>
            </td>
            <td><span :class="statusBadge[kb.status] || 'badge badge-gray'" :title="kb.status">{{ t(KB_STATUS_TEXT, kb.status) }}</span></td>
            <td>{{ kb.source_count }}</td>
            <td>{{ kb.page_count }}</td>
            <td class="font-mono text-xs">{{ kb.current_rev || '—' }}</td>
            <td>{{ kb.binding_count }}</td>
            <td class="text-xs text-t3">{{ fmtTime(kb.updated_at) }}</td>
            <td class="whitespace-nowrap">
              <button class="btn btn-text" @click.stop="open(kb)">详情</button>
              <button v-if="canManage" class="btn btn-text text-red-500" :disabled="deleting === kb.kb_id" @click.stop="removeKb(kb)">删除</button>
            </td>
          </tr>
        </tbody>
      </table>
      <Pagination :pager="kbsPager" />
    </div>

    <!-- 新建 -->
    <div v-if="showForm" class="fixed inset-0 bg-black/30 flex items-center justify-center z-50" @click.self="showForm = false">
      <div class="bg-white rounded-xl p-6 w-[560px] shadow-xl">
        <h3 class="font-semibold mb-4">新建知识库</h3>
        <div class="space-y-3">
          <div class="grid grid-cols-2 gap-3">
            <div><label class="text-xs text-t2">标识（英文小写，建后不便更改；Agent 看到的工具名由它生成）</label><input v-model="form.slug" class="input" placeholder="return-policy" /></div>
            <div><label class="text-xs text-t2">名称（Agent 靠它判断该不该查这个库，写具体些）</label><input v-model="form.display_name" class="input" placeholder="退换货政策知识库" /></div>
          </div>
          <div><label class="text-xs text-t2">描述（仅供人看，不参与逻辑）</label><input v-model="form.description" class="input" /></div>
          <div>
            <label class="text-xs text-t2">密级（仅作标注，当前不做访问拦截）</label>
            <select v-model="form.classification" class="input"><option>PUBLIC</option><option>INTERNAL</option><option>CONFIDENTIAL</option></select>
          </div>
          <div><label class="text-xs text-t2">目录规则 · schema.md —— 决定文档放进哪个目录。每行「- 目录名: 关键词1 关键词2」，关键词命中文档标题即归入该目录</label><textarea v-model="form.schema_text" rows="4" class="input font-mono"></textarea></div>
          <div><label class="text-xs text-t2">写作要求 · purpose.md —— 生成页面正文时大模型会遵守的规矩，比如「时限金额必须给确切数字」</label><textarea v-model="form.purpose_text" rows="3" class="input"></textarea></div>
        </div>
        <div class="flex justify-end gap-2 mt-4">
          <button class="btn btn-ghost" @click="showForm = false">取消</button>
          <button class="btn btn-primary" @click="submit">创建</button>
        </div>
      </div>
    </div>
  </div>

  <!-- ============ 详情 ============ -->
  <div v-else>
    <div class="flex items-center gap-3 mb-4">
      <button class="btn btn-ghost btn-sm" @click="back">← 返回</button>
      <h2 class="text-xl font-semibold">{{ detail.display_name }}</h2>
      <span :class="statusBadge[detail.status] || 'badge badge-gray'" :title="detail.status">{{ t(KB_STATUS_TEXT, detail.status) }}</span>
      <span class="text-xs text-t3 font-mono">{{ detail.slug }} · {{ detail.current_rev || '尚未编译' }}</span>
      <button v-if="canManage" class="btn btn-text text-red-500 ml-auto" :disabled="deleting === detail.kb_id" @click="removeKb(detail)">删除知识库</button>
    </div>

    <div class="flex gap-1 border-b border-line mb-5 flex-wrap">
      <button v-for="t in [['overview','概览'],['sources','原始文档'],['plans','整理方案'],['pages','生成的页面'],['schema','规则设置'],['search','检索测试'],['reviews','质量问题'],['tasks','编译记录'],['bindings','绑定 Agent']]" :key="t[0]"
              :class="['px-4 h-10 text-sm font-medium border-b-2 -mb-px cursor-pointer', tab === t[0] ? 'border-brand text-brand' : 'border-transparent text-t2 hover:text-t1']"
              @click="tab = t[0]; t[0]==='bindings' && loadBindings()">{{ t[1] }}</button>
    </div>

    <!-- 概览 -->
    <div v-if="tab === 'overview'" class="card">
      <div class="grid grid-cols-4 gap-4 mb-4">
        <div><div class="text-xs text-t3">原始文档</div><div class="text-2xl font-semibold">{{ detail.source_count }}</div></div>
        <div><div class="text-xs text-t3">生成页面</div><div class="text-2xl font-semibold">{{ detail.page_count }}</div></div>
        <div><div class="text-xs text-t3">被几个 Agent 使用</div><div class="text-2xl font-semibold">{{ detail.binding_count }}</div></div>
        <div><div class="text-xs text-t3">密级</div><div class="text-lg font-semibold mt-1">{{ detail.classification }}</div></div>
      </div>
      <div class="text-sm text-t2 mb-4">{{ detail.description || '（无描述）' }}</div>
      <div class="flex gap-2 flex-wrap">
        <button v-if="canManage" class="btn btn-primary" title="跑完整流程：解析→切块→生成正文→质检→发布新版本" @click="trigger('INGEST')">增量编译</button>
        <button v-if="canManage" class="btn btn-ghost" title="不重写正文，只拿现有页面重新切块建索引；改过分块参数后用" @click="trigger('REBUILD')">重建索引</button>
        <button v-if="canManage" class="btn btn-ghost" title="只给没切块或出错的页面补切块，不发布新版本；修复用" @click="trigger('EMBED')">补切块</button>
        <span class="text-xs text-t3 self-center">编译在后台跑，进度看「编译记录」</span>
      </div>
    </div>

    <!-- 原始文档 -->
    <div v-if="tab === 'sources'" class="card">
      <!-- 上传区：点击或拖拽，支持多选 -->
      <input ref="fileInput" type="file" multiple :accept="ACCEPT" class="hidden" @change="onUpload" />
      <div
        :class="['rounded-xl border-2 border-dashed transition-colors mb-5 px-6 py-8 text-center',
                 uploadBusy ? 'border-line bg-canvas cursor-wait'
                 : dragging ? 'border-brand bg-brand-lighter cursor-copy'
                 : 'border-line bg-canvas hover:border-brand hover:bg-brand-lighter cursor-pointer']"
        @click="!uploadBusy && pickFile()"
        @dragover.prevent="dragging = true"
        @dragenter.prevent="dragging = true"
        @dragleave.prevent="dragging = false"
        @drop.prevent="onDrop">

        <template v-if="uploadBusy">
          <div class="text-sm font-medium text-t1">正在上传…</div>
          <div class="text-xs text-t3 mt-1.5 font-mono">{{ uploadProgress }}</div>
        </template>

        <template v-else>
          <svg class="w-9 h-9 mx-auto mb-3 text-t3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
            <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4" />
            <path d="M17 8l-5-5-5 5" /><path d="M12 3v13" />
          </svg>
          <div class="text-base font-medium text-t1 mb-1">上传文档</div>
          <div class="text-sm text-t2 mb-4">把文件拖到这里，或者</div>
          <button v-if="canManage" class="btn btn-primary" @click.stop="pickFile">选择文件</button>
          <div class="text-xs text-t3 mt-4">
            支持 .md / .txt / .json / .csv / .pdf，单个不超过 20MB，可一次选多份
          </div>
        </template>
      </div>

      <div class="text-xs text-t3 mb-4">
        上传后自动生成「整理方案」（30 秒内的多份会合并成一次）。
        <span class="text-t2">方案需要你确认后才会编译成页面</span>，等不及可在下方点「重新整理」立即触发。
      </div>
      <table class="tbl">
        <thead><tr><th>文件</th><th>状态</th><th>内容指纹</th><th>最后编译进哪版</th><th>上传时间</th><th></th></tr></thead>
        <tbody>
          <tr v-if="!sources.length"><td colspan="6" class="text-center text-t3 py-8">
            还没有上传任何文档 —— 用上方的上传区添加第一份
          </td></tr>
          <tr v-for="s in sources" :key="s.source_id">
            <td class="font-mono text-xs">{{ s.file_name }}<div class="text-t3">{{ s.rel_path }}</div></td>
            <td><span :class="{ DONE: 'badge badge-ok', PENDING: 'badge badge-warn', PLANNING: 'badge badge-warn', FAILED: 'badge badge-err' }[s.status] || 'badge badge-gray'" :title="s.status">{{ t(SRC_STATUS_TEXT, s.status) }}</span></td>
            <td class="font-mono text-xs text-t3">{{ s.sha256?.slice(0, 12) }}…</td>
            <td class="font-mono text-xs">{{ s.last_rev || '—' }}</td>
            <td class="text-xs text-t3">{{ fmtTime(s.created_at) }}</td>
            <td class="whitespace-nowrap">
              <button v-if="canManage" class="btn btn-text" title="跳过 30 秒等待，立刻重新生成整理方案" @click="replan(s)">重新整理</button>
              <button v-if="canManage" class="btn btn-text text-red-500" @click="removeSource(s)">删除</button>
            </td>
          </tr>
        </tbody>
      </table>
      <Pagination :pager="sourcesPager" />
    </div>

    <!-- 生成的页面 -->
    <div v-if="tab === 'pages'" class="card !p-0">
      <table class="tbl">
        <thead><tr><th>页面路径</th><th>标题</th><th>分块数</th><th>已切块</th><th>更新时间</th></tr></thead>
        <tbody>
          <tr v-if="!pages.length"><td colspan="5" class="text-center text-t3 py-8">还没有页面。上传文档并确认整理方案后，编译会在这里生成页面</td></tr>
          <tr v-for="p in pages" :key="p.page_id">
            <td class="font-mono text-xs">{{ p.rel_path }}</td>
            <td>{{ p.title }}</td>
            <td>{{ p.chunk_count }}</td>
            <td><span :class="p.embedded ? 'badge badge-ok' : 'badge badge-gray'">{{ p.embedded ? '是' : '否' }}</span></td>
            <td class="text-xs text-t3">{{ fmtTime(p.updated_at) }}</td>
          </tr>
        </tbody>
      </table>
      <Pagination :pager="pagesPager" />
    </div>

    <!-- 规则设置 -->
    <div v-if="tab === 'schema'" class="card">
      <div class="flex justify-between items-center mb-3">
        <h3 class="font-semibold">目录规则与写作要求</h3>
        <div>
          <button v-if="canManage && !schemaEdit" class="btn btn-ghost btn-sm" @click="editSchema">编辑</button>
          <template v-else>
            <button class="btn btn-ghost btn-sm" @click="schemaEdit = false">取消</button>
            <button class="btn btn-primary btn-sm ml-2" @click="saveSchema">保存</button>
          </template>
        </div>
      </div>
      <template v-if="!schemaEdit">
        <div class="text-xs text-t3 mb-1">目录规则 · schema.md —— 决定文档归入哪个目录（关键词匹配文档标题，从上往下第一个命中的生效）</div>
        <pre class="bg-canvas rounded-lg p-3 text-xs whitespace-pre-wrap mb-4">{{ detail.schema_text || '（空）' }}</pre>
        <div class="text-xs text-t3 mb-1">写作要求 · purpose.md —— 生成页面正文时大模型遵守的规矩</div>
        <pre class="bg-canvas rounded-lg p-3 text-xs whitespace-pre-wrap">{{ detail.purpose_text || '（空）' }}</pre>
      </template>
      <template v-else>
        <div class="text-xs text-t3 mb-1">目录规则 · schema.md（每行「- 目录名: 关键词1 关键词2」）</div>
        <textarea v-model="schemaDraft.schema_text" rows="6" class="input font-mono mb-3"></textarea>
        <div class="text-xs text-t3 mb-1">写作要求 · purpose.md</div>
        <textarea v-model="schemaDraft.purpose_text" rows="4" class="input"></textarea>
      </template>
    </div>

    <!-- 整理方案（T21）-->
    <div v-if="tab === 'plans'" class="space-y-4">
      <div v-if="!plans.length" class="card text-center py-10">
        <div class="text-t2 text-sm">还没有待处理的规划</div>
        <div class="text-t3 text-xs mt-2">
          刚上传的文档会在 30 秒内自动生成规划，本页每 5 秒刷新一次，稍等即可。<br />
          不想等就去「原始文档」点该文件的「重新整理」立即触发。
        </div>
      </div>

      <div v-for="plan in plans" :key="plan.plan_id" class="card"
           :class="plan.status === 'DRAFT' ? '!border-2 !border-brand' : ''">
        <div class="flex items-center justify-between mb-3">
          <div class="flex items-center gap-2">
            <span class="font-mono text-xs text-t3">{{ plan.plan_id }}</span>
            <span :class="{ DRAFT: 'badge badge-warn', CONFIRMED: 'badge badge-brand', APPLIED: 'badge badge-ok', APPLIED_WITH_SKIPS: 'badge badge-warn', REJECTED: 'badge badge-err', SUPERSEDED: 'badge badge-gray' }[plan.status]" :title="plan.status">{{ t(PLAN_STATUS_TEXT, plan.status) }}</span>
            <span v-if="plan.proposal?.confidence" class="badge badge-gray">置信度: {{ plan.proposal.confidence }}</span>
            <span v-if="plan.applied_rev" class="font-mono text-xs">→ {{ plan.applied_rev }}</span>
          </div>
          <div v-if="plan.status === 'DRAFT'" class="flex gap-2">
            <button v-if="canManage" class="btn btn-primary btn-sm" @click="confirmPlan(plan)">确认并开始编译</button>
            <button v-if="canManage" class="btn btn-ghost btn-sm" @click="rejectPlan(plan)">驳回</button>
          </div>
        </div>

        <!-- placement -->
        <div v-if="plan.proposal?.placement" class="bg-brand-light/40 rounded-lg p-3 mb-3 text-sm">
          <div class="font-medium mb-1">兜底目录（未命中任何关键词的文档会放这里；实际去向见下方清单）</div>
          <div class="font-mono text-xs text-t2">{{ plan.proposal.placement.proposed_dir }}</div>
          <div class="text-t2 mt-1">{{ plan.proposal.placement.rationale }}</div>
        </div>

        <!-- changes 勾选（依赖闭包 D4）-->
        <div v-if="plan.status === 'DRAFT'" class="mb-3">
          <div class="text-xs text-t2 font-medium mb-2">机器建议这样整理（可只勾选一部分；被依赖的动作会自动补勾）</div>
          <label v-for="c in plan.proposal.changes" :key="c.action_id"
                 class="flex items-start gap-2 p-2 rounded hover:bg-canvas cursor-pointer">
            <input v-if="c.action !== 'UPDATE_INDEX_LOG'" type="checkbox" class="mt-1" :checked="isSelected(plan, c.action_id)" @change="toggle(plan, c.action_id)" />
            <span v-else class="mt-1 w-4"></span>
            <div class="min-w-0 text-sm">
              <div>
                <span class="font-medium">{{ t(ACTION_TEXT, c.action) }}</span>
                <span class="font-mono text-xs text-t2 ml-1.5">{{ c.path || c.target || '' }}</span>
                <span v-if="c.depends_on?.length" class="text-xs text-t3 ml-1">（需先执行：{{ c.depends_on.join('、') }}）</span>
              </div>
              <div class="text-t2">{{ c.effect || c.reason || '' }}</div>
              <div v-if="c.result" class="text-xs mt-0.5" :class="c.result.status === 'APPLIED' ? 'text-up' : 'text-warn'">
                执行结果：{{ c.result.status === 'APPLIED' ? '已完成' : '已跳过' }} — {{ c.result.detail }}
              </div>
            </div>
          </label>
        </div>
        <div v-else class="mb-3 text-sm">
          <div v-for="c in plan.proposal.changes" :key="c.action_id" class="text-xs text-t2 mb-1">
            {{ t(ACTION_TEXT, c.action) }} <span class="font-mono">{{ c.path || c.target || '' }}</span>
            <span v-if="c.result" :class="c.result.status === 'APPLIED' ? 'text-up' : 'text-warn'">
              → {{ c.result.status === 'APPLIED' ? '已完成' : '已跳过' }}（{{ c.result.detail }}）</span>
          </div>
        </div>

        <!-- preview diff -->
        <div v-if="plan.preview" class="grid grid-cols-3 gap-3 mb-3 text-xs">
          <div class="bg-canvas rounded p-2"><div class="text-t3 mb-1">会新建这些页面</div>
            <div v-for="n in plan.preview.new_pages" :key="n.path" class="font-mono text-up">+ {{ n.path }}</div>
          </div>
          <div class="bg-canvas rounded p-2"><div class="text-t3 mb-1">会改动这些页面</div>
            <div v-for="u in plan.preview.updated_pages" :key="u.path" class="font-mono text-warn">~ {{ u.path }}（{{ u.change_summary }}）</div>
            <div v-if="!plan.preview.updated_pages.length" class="text-t3">—</div>
          </div>
          <div class="bg-canvas rounded p-2"><div class="text-t3 mb-1">页面间链接调整</div>
            <div v-for="r in plan.preview.relinks" :key="r.to" class="font-mono">{{ r.from }} → {{ r.to }}（{{ r.count }} 处）</div>
            <div v-if="!plan.preview.relinks.length" class="text-t3">—</div>
          </div>
        </div>

        <details class="text-sm">
          <summary class="cursor-pointer text-t2">展开：判断依据 / 风险提示 / 备选方案</summary>
          <pre class="bg-canvas rounded-lg p-3 text-xs whitespace-pre-wrap mt-2">{{ plan.rationale_md }}</pre>
          <pre v-if="plan.risk_md" class="bg-canvas rounded-lg p-3 text-xs whitespace-pre-wrap mt-2">{{ plan.risk_md }}</pre>
          <div v-for="(a, i) in plan.proposal?.alternatives || []" :key="i" class="text-xs text-t2 mt-2">
            备选做法：{{ a.rationale }}（代价：{{ a.tradeoff }}）
          </div>
        </details>
      </div>
      <Pagination :pager="plansPager" />
      <div v-if="draftPlans.length" class="text-xs text-t3">{{ CONFIRM_NOTICE }}</div>
    </div>

    <!-- 检索测试 -->
    <div v-if="tab === 'search'" class="card">
      <div class="flex gap-2 mb-4">
        <input v-model="searchQ" class="input" placeholder="输入查询，如：七天无理由可以退吗" @keyup.enter="doSearch" />
        <button class="btn btn-primary" :disabled="searching" @click="doSearch">检索</button>
      </div>
      <div v-if="searchResult">
        <div class="flex items-center gap-3 text-xs text-t3 mb-3">
          <span>版本 {{ searchResult.rev }}</span>
          <span>返回内容 {{ searchResult.window.used_bytes }}/{{ searchResult.window.budget_bytes }} 字节（最多 {{ searchResult.window.limit }} 条）</span>
          <span>关键词命中 {{ searchResult.keyword_hits }} 页 · 链接图谱补充 {{ searchResult.graph_hits }} 页</span><span>相关度 {{ searchResult.top_keyword_score }} / 拒答线 {{ searchResult.keyword_floor }}</span>
          <span v-if="searchResult.no_reliable_hit" class="badge badge-err">相关度不足 · Agent 应拒答</span>
        </div>
        <div v-if="searchResult.no_reliable_hit" class="text-sm text-warn bg-warn-light/30 rounded-lg p-3">
          首位结果的相关度低于本库的拒答线，说明库里没有能回答这个问题的资料。Agent 应当明确告知「没有可靠资料」，而不是拿这些低相关内容硬答。
        </div>
        <div v-for="r in searchResult.results" :key="r.page_id" class="border border-line rounded-lg p-3 mb-2">
          <div class="flex items-center gap-2 text-sm mb-1">
            <span class="font-medium">{{ r.title }}</span>
            <span class="font-mono text-xs text-t3">{{ r.path }}</span>
            <span class="badge badge-gray">排序分 {{ r.score }}</span>
          </div>
          <pre class="text-xs whitespace-pre-wrap text-t2 bg-canvas rounded p-2">{{ r.snippet }}</pre>
          <div class="text-xs text-t3 mt-1">出自原始文档：{{ r.sources.join('、') || '—' }}</div>
        </div>
      </div>
    </div>

    <!-- Review -->
    <div v-if="tab === 'reviews'" class="card !p-0">
      <table class="tbl">
        <thead><tr><th>问题类型</th><th>问题详情</th><th>出现在哪个页面</th><th>状态</th><th></th></tr></thead>
        <tbody>
          <tr v-if="!reviews.length"><td colspan="5" class="text-center text-t3 py-8">没有待处理的质量问题</td></tr>
          <tr v-for="r in reviews" :key="r.review_id">
            <td><span :class="{ contradiction: 'badge badge-err', duplicate: 'badge badge-warn', missing_page: 'badge badge-warn', suggestion: 'badge badge-gray' }[r.type]" :title="r.type">{{ t(REVIEW_TYPE_TEXT, r.type) }}</span></td>
            <td class="text-sm">{{ r.payload?.evidence }}</td>
            <td class="font-mono text-xs text-t3">{{ (r.payload?.locations || []).join(', ') }}</td>
            <td>待处理</td>
            <td class="whitespace-nowrap">
              <button v-if="canManage" class="btn btn-text" title="问题已修复，从队列移除" @click="resolveReview(r, 'RESOLVED')">已处理</button>
              <button v-if="canManage" class="btn btn-text text-t3" title="这不算问题，不用管" @click="resolveReview(r, 'DISMISSED')">忽略</button>
            </td>
          </tr>
        </tbody>
      </table>
      <Pagination :pager="reviewsPager" />
    </div>

    <!-- 编译记录（T20：5s 轮询 + 取消）-->
    <div v-if="tab === 'tasks'" class="space-y-4">
      <div class="card !p-0">
        <div class="px-4 pt-4 font-semibold text-sm">编译任务<span class="font-normal text-t3 ml-2">进行中时每 5 秒自动刷新</span></div>
        <table class="tbl">
          <thead><tr><th>任务编号 / 产出版本</th><th>类型</th><th>状态</th><th>当前进度</th><th>重试次数</th><th>开始时间</th><th></th></tr></thead>
          <tbody>
            <tr v-if="!tasks.length"><td colspan="7" class="text-center text-t3 py-6">暂无任务</td></tr>
            <tr v-for="t in tasks" :key="t.task_id">
              <td class="font-mono text-xs">{{ t.task_id }}<div class="text-t3">{{ t.rev }}</div></td>
              <td :title="t.kind">{{ TASK_KIND_TEXT[t.kind] || t.kind }}</td>
              <td><span :class="{ DONE: 'badge badge-ok', RUNNING: 'badge badge-warn', PENDING: 'badge badge-gray', FAILED: 'badge badge-err', CANCELED: 'badge badge-gray' }[t.status]" :title="t.status">{{ TASK_STATUS_TEXT[t.status] || t.status }}</span></td>
              <td class="text-xs text-t2">{{ t.progress_msg || '—' }}</td>
              <td>{{ t.retry_count }}</td>
              <td class="text-xs text-t3">{{ fmtTime(t.created_at) }}</td>
              <td>
                <button v-if="canManage && ['PENDING', 'RUNNING'].includes(t.status)" class="btn btn-text text-red-500" @click="cancelTask(t)">取消</button>
              </td>
            </tr>
          </tbody>
        </table>
      <Pagination :pager="tasksPager" />
      </div>
      <div class="card !p-0">
        <div class="px-4 pt-4 font-semibold text-sm">已发布版本<span class="font-normal text-t3 ml-2">每次编译成功发布一个，整库原子切换</span></div>
        <table class="tbl">
          <thead><tr><th>版本号</th><th>状态</th><th>本次变更</th><th>发布时间</th></tr></thead>
          <tbody>
            <tr v-for="r in revs" :key="r.rev">
              <td class="font-mono text-xs">{{ r.rev }}<span v-if="r.rev === detail.current_rev" class="badge badge-brand ml-2">对外服务中</span></td>
              <td><span :class="{ READY: 'badge badge-ok', BUILDING: 'badge badge-warn', FAILED: 'badge badge-err' }[r.status]" :title="r.status">{{ t(REV_STATUS_TEXT, r.status) }}</span></td>
              <td class="text-xs text-t2">新建 {{ r.summary?.pages_created ?? '—' }} 页 · 更新 {{ r.summary?.pages_updated ?? '—' }} 页 · 质检发现 {{ r.report?.lint_issues ?? '—' }} 个问题</td>
              <td class="text-xs text-t3">{{ fmtTime(r.created_at) }}</td>
            </tr>
          </tbody>
        </table>
      <Pagination :pager="revsPager" />
      </div>
    </div>

    <!-- 绑定 Agent -->
    <div v-if="tab === 'bindings'" class="card">
      <div class="flex gap-2 mb-4">
        <select v-model="bindMasterId" class="input max-w-xs" @change="pickMaster">
          <option value="">先选一个 Agent Team…</option>
          <option v-for="m in masters" :key="m.master_id" :value="m.master_id">{{ m.display_name }}（标识：{{ m.slug }}）</option>
        </select>
        <span class="text-xs text-t3 self-center">绑定后立刻生效，不用重启。Agent 只能检索，不能修改知识库内容</span>
      </div>
      <template v-if="bindMasterId">
        <table class="tbl">
          <thead><tr><th>知识库名称</th><th>标识</th><th>Agent 看到的工具名</th><th>操作</th></tr></thead>
          <tbody>
            <tr v-for="kb in bindingKbs" :key="kb.kb_id">
              <td>{{ kb.display_name }}</td>
              <td class="font-mono text-xs">{{ kb.slug }}</td>
              <td class="font-mono text-xs text-t3">kb_search_{{ kb.slug }}</td>
              <td>
                <button v-if="canManage" :class="boundKb(kb.kb_id) ? 'btn btn-ghost btn-sm' : 'btn btn-primary btn-sm'" @click="toggleBind(kb)">
                  {{ boundKb(kb.kb_id) ? '解绑' : '绑定' }}
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </template>
    </div>
  </div>
</template>
