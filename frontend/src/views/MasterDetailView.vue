<script setup>
import { ref, computed, onMounted } from 'vue'
import { api } from '../api.js'
import { fmtDateTime } from '../format.js'
import { getSession } from '../auth.js'
import { success } from '../notify.js'
import { confirmAction } from '../ui.js'
import RunResultPanel from '../components/RunResultPanel.vue'
import RunHistoryGroups from '../components/RunHistoryGroups.vue'
import { sendA2aMessage, cancelA2aTask, reconnectA2aTask } from '../a2a-client.js'
import { parseReactConfig, reactConfigRequest } from '../react-config.js'

import Pagination from '../components/Pagination.vue'
import { useServerPagination } from '../pagination.js'

const props = defineProps({
  masterId: String,
  initialTab: { type: String, default: 'bindings' },
  canManage: { type: Boolean, default: false },
  canLoopManage: { type: Boolean, default: false },
})
const emit = defineEmits(['back', 'open-trace'])

const detail = ref(null)
const services = ref([])
const err = ref('')
const tab = ref(props.initialTab)
const detailTabs = computed(() => [
  ['bindings', '绑定服务'], ['kb', '知识库'], ['prompts', '提示词版本'],
  ['loop', '循环配置'], ['model', '模型配置'], ['perm', '权限'],
  ...(props.canManage ? [['testrun', '试运行']] : []), ['runs', '运行监控'],
])
// 每个保存动作独立的进行中标记，避免点了没反应 / 重复提交
const saving = ref('')

// 绑定编辑状态
const bindSelection = ref({}) // service_id -> {checked, toolName, toolDescription}

// 知识库绑定：与 A2A 服务绑定并列，共同构成该 Agent 的可用工具集。
// 此前本页只渲染服务绑定，知识库只能在「知识库」页绑，Agent 侧完全看不到，
// 导致「知识库显示被引用、Agent 页却查无此事」的困惑。
const kbs = ref([])
const kbSelection = ref({})   // kb_id -> checked
const kbSaved = ref("")
// 服务端快照，用于判断「有未保存修改」。原先徽标读的是本地勾选状态，用户一勾
// 徽标就变了，点保存后界面零变化，看起来就是「没反应」。
const bindSaved = ref('')

// 提示词编辑
const promptDraft = ref('')
const prompts = ref([])
const currentPromptId = ref(null)

// 循环配置
const maxIters = ref(8)
const responseMode = ref('SYNTHESIZE')

// 模型配置（master-model-config 专题）
const modelCfg = ref({ baseUrl: '', model: '', apiKey: '' })
const modelCfgSet = ref(false)

// 运行监控：全部来源的调用记录（A2A 端点 / 管理台试运行 / 性能测试）。
// 试运行历史只是它按来源过滤后的一个子集，两边共用同一套渲染。
const runs = ref([])
const runSource = ref('all')        // all | a2a | testrun | perf
const runOnlyProblems = ref(false)  // 只看非 SUCCEEDED，排障第一屏
const runResult = ref(null)         // 展开回看的那条
const RUN_SOURCES = [
  ['all', '全部来源'], ['a2a', 'A2A 端点'],
  ['testrun', '管理台试运行'], ['perf', '性能测试'],
]

// 管理台试运行通过该 Team 的 A2A 端点发送消息。
const trText = ref('')
const trContextId = ref('')   // 空 = 每次新开上下文；有值 = 接着上一轮追问
const trResult = ref(null)
const trErr = ref('')
// 按服务传入额外业务参数。
const trServices = ref('{}')
// 历史来自服务端 run_record（不是浏览器本地缓存）：切走页面、换台机器再进来
// 都还在。后端按 context_id 前缀过滤，只取试运行自己那批。
const trHistory = ref([])
const TESTRUN_CTX_PREFIX = 'testrun-'
const historyRows = ref([])
const promptsPager = useServerPagination(prompts, () => `/api/v1/agents/${props.masterId}/prompts?with_text=true`, {
  onResult: result => { currentPromptId.value = result.current_prompt_version_id },
})
const runsPager = useServerPagination(runs, () => `/api/v1/agents/${props.masterId}/runs?source=${runSource.value}&only_problems=${runOnlyProblems.value}`, {
  onResult: () => { runResult.value = null },
})
const historyPager = useServerPagination(historyRows, () => `/api/v1/agents/${props.masterId}/runs?source=testrun`, {
  onResult: result => { trHistory.value = result.items.map(row => ({ q: strippedQuestion(row.input_text), r: runRecordToResult(row) })) },
})

async function load() {
  err.value = ''
  try {
    detail.value = await api.getMaster(props.masterId)
    const resp = await api.listPrompts(props.masterId, true)
    await promptsPager.load()
    currentPromptId.value = resp.current_prompt_version_id
    const published = (resp.prompts || []).find(p => p.prompt_version_id === resp.current_prompt_version_id)
    promptDraft.value = published?.prompt_text || ''
    const reactConfig = parseReactConfig(detail.value.master.react_config_json)
    maxIters.value = reactConfig.maxIters
    responseMode.value = reactConfig.responseMode
    const mc = detail.value.master.model_config
    modelCfgSet.value = !!mc
    modelCfg.value = { baseUrl: mc?.base_url || '', model: mc?.model || '', apiKey: '' }
    // 可绑定服务 = 已注册服务
    const serviceResult = await api.serviceSelector('multi')
    services.value = serviceResult.items.map(s => ({
      service_id: s.serviceId, slug: s.slug, display_name: s.displayName,
      source_type: s.sourceType, enabled: s.enabled,
    }))
    const sel = {}
    for (const s of services.value) {
      const bound = (detail.value.bindings || []).find(b => b.service_id === s.service_id)
      sel[s.service_id] = {
        checked: !!bound,
        toolName: bound?.tool_name || s.slug,
        toolDescription: bound?.tool_description || s.display_name,
        interactionMode: bound?.interaction_mode || 'TOOL',
      }
    }
    bindSelection.value = sel
    bindSaved.value = bindFingerprint()
    // 知识库：全量列表 + 该 Agent 已绑定集合
    const [kbResp, kbBind] = await Promise.all([
      api.kbSelector('multi'), api.getKbBindings(props.masterId),
    ])
    kbs.value = kbResp.items.map(k => ({
      kb_id: k.kbId, slug: k.slug, display_name: k.displayName, status: k.status,
    }))
    const boundIds = new Set(kbBind.bindings.map(b => b.kb_id))
    kbSelection.value = Object.fromEntries(
      kbs.value.map(k => [k.kb_id, boundIds.has(k.kb_id)]),
    )
    kbSaved.value = kbFingerprint()
    await loadRuns()
    await loadTestRunHistory()
  } catch (e) {
    err.value = e.message
  }
}
onMounted(async () => { await load(); if (props.canLoopManage) loadEvalDatasets() })

/** 绑定的规范化指纹：与服务端快照比对以判断是否有未保存修改。 */
function bindFingerprint() {
  return JSON.stringify(
    Object.entries(bindSelection.value)
      .filter(([, v]) => v.checked)
      .map(([sid, v]) => [sid, v.toolName, v.toolDescription, v.interactionMode])
      .sort((a, b) => a[0].localeCompare(b[0])),
  )
}

const boundCount = computed(() =>
  Object.values(bindSelection.value).filter(v => v.checked).length)
const bindDirty = computed(() => bindFingerprint() !== bindSaved.value)

function kbFingerprint() {
  return JSON.stringify(
    Object.entries(kbSelection.value).filter(([, on]) => on)
      .map(([id]) => id).sort(),
  )
}
const kbBoundCount = computed(() =>
  Object.values(kbSelection.value).filter(Boolean).length)
const kbDirty = computed(() => kbFingerprint() !== kbSaved.value)

/** 运行记录只存工具名；用当前绑定补回面向人的业务名称。 */
const runToolLabels = computed(() => {
  const labels = {}
  for (const binding of detail.value?.bindings || []) {
    labels[binding.tool_name] = binding.service_name || binding.service_slug || binding.tool_name
  }
  for (const kb of kbs.value || []) {
    if (!kbSelection.value[kb.kb_id]) continue
    labels[`kb_search_${kb.slug}`] = `搜索：${kb.display_name}`
    labels[`kb_read_${kb.slug}`] = `细读：${kb.display_name}`
    labels[`kb_graph_${kb.slug}`] = `图谱：${kb.display_name}`
  }
  return labels
})
function openTrace(traceId) {
  const labels = {
    testrun: '返回试运行',
    runs: '返回运行监控',
  }
  emit('open-trace', {
    traceId,
    returnTab: tab.value,
    returnLabel: labels[tab.value] || '返回 Agent 详情',
  })
}

async function saveKbBindings() {
  if (!kbDirty.value) return
  saving.value = 'kb'
  try {
    // 覆盖式提交：服务端按这份全集重写该 Agent 的知识库绑定
    const bindings = Object.entries(kbSelection.value)
      .filter(([, on]) => on)
      .map(([kb_id]) => ({ kb_id, enabled: true }))
    await api.putKbBindings(props.masterId, bindings)
    await load()
    success(`已保存：该 Agent 可检索 ${bindings.length} 个知识库（立即生效，无需重启）`)
  } catch { /* toast 已展示 */ } finally {
    saving.value = ''
  }
}

async function saveBindings() {
  if (!bindDirty.value) return
  saving.value = 'bindings'
  try {
    const bindings = Object.entries(bindSelection.value)
      .filter(([, v]) => v.checked)
      .map(([serviceId, v]) => ({
        serviceId, toolName: v.toolName, toolDescription: v.toolDescription,
        interactionMode: v.interactionMode,
      }))
    await api.putBindings(props.masterId, bindings)
    await load()
    success(`绑定已保存：${bindings.length} 个服务，Agent Card 已同步刷新`)
  } catch { /* toast 已展示 */ } finally {
    saving.value = ''
  }
}

async function savePrompt() {
  saving.value = 'prompt'
  try {
    await api.createPrompt(props.masterId, promptDraft.value)
    await load()
    success(`已保存为新版本 v${prompts.value[0]?.version_no ?? '?'}（DRAFT，需发布后生效）`)
  } catch { /* toast 已展示 */ } finally {
    saving.value = ''
  }
}

async function publish(vid) {
  if (!await confirmAction('发布后立即热生效（无需重启后端），确认发布该版本？',
                           { okText: '发布' })) return
  saving.value = `publish:${vid}`
  try {
    await api.publishPrompt(props.masterId, vid)
    await load()
    success('提示词已发布，下一次运行即生效')
  } catch { /* toast 已展示 */ } finally {
    saving.value = ''
  }
}

async function saveIterConfig() {
  saving.value = 'loop'
  try {
    await api.updateReactConfig(
      props.masterId,
      reactConfigRequest(maxIters.value, responseMode.value),
    )
    await load()
    success('运行配置已保存')
  } catch { /* toast 已展示 */ } finally {
    saving.value = ''
  }
}

async function saveModelConfig() {
  saving.value = 'model'
  try {
    await api.updateModelConfig(props.masterId, {
      baseUrl: modelCfg.value.baseUrl,
      model: modelCfg.value.model,
      apiKey: modelCfg.value.apiKey || null,
    })
    await load()
    success('模型配置已保存')
  } catch { /* toast 已展示 */ } finally {
    saving.value = ''
  }
}

async function clearModelConfig() {
  if (!await confirmAction(
    '将清除这个 Team 自己的模型配置，改用平台全局默认模型。',
    { okText: '恢复默认', danger: true })) return
  saving.value = 'model'
  try {
    await api.clearModelConfig(props.masterId)
    await load()
    success('已恢复平台默认模型')
  } catch { /* toast 已展示 */ } finally {
    saving.value = ''
  }
}

function parseJsonField(raw, fallback) {
  if (Array.isArray(raw) || (raw && typeof raw === 'object')) return raw
  try { return JSON.parse(raw || '') } catch { return fallback }
}

/** 剥掉 A2A 入站 framing 前缀，只留用户真正问的那句。 */
function strippedQuestion(inputText) {
  const parts = String(inputText || '').split(']\n\n')
  return parts[parts.length - 1].trim()
}

/**
 * run_record 行 → 试运行结果结构。
 *
 * 两个字段 run_record 里没有，恢复出来的记录必然缺：
 * - toolbox：工具箱快照是调用当时算出来的，不入库（绑定/权限随时会变，
 *   事后重算也不等于当时的真相，与其给个可能骗人的值不如给 null）
 * - inputRequired：澄清握手标记未持久化，恢复的记录不显示「追问」徽标
 * elapsedMs 由起止时间戳算，和实时返回的值有毫秒级出入，够用。
 */
function runRecordToResult(row) {
  const started = row.started_at ? new Date(row.started_at).getTime() : null
  const finished = row.finished_at ? new Date(row.finished_at).getTime() : null
  return {
    runId: row.run_id,
    status: row.status,
    errorCode: row.error_code,
    errorDetail: row.error_detail,
    output: row.final_output || '',
    inputRequired: false,
    routedServices: parseJsonField(row.routed_services, []),
    toolCalls: parseJsonField(row.tool_calls, []),
    elapsedMs: (started && finished) ? Math.max(0, finished - started) : null,
    // 阶段耗时拆解（后端 012 之后才有；老记录为 null，面板自动不显示）
    timing: parseJsonField(row.timing_json, null),
    promptVersionId: row.prompt_version_id,
    traceId: row.trace_id,
    contextId: row.context_id,
    asUser: row.cap_user_name,
    toolbox: null,
    restored: true,
    startedAt: row.started_at,
  }
}

/** 从服务端拉回本 Team 的试运行历史（切页面/换机器都不会丢）。 */
async function loadTestRunHistory() {
  await historyPager.load()
  // 故意不自动回填 contextId：回来后随手问个新问题却落进旧对话，会被上一轮
  // 的历史带偏（实测过：上一轮问天气，这一轮问股价，模型会拿旧的天气结果作答）。
  // 要接着聊就点历史行上的「以此继续」，是显式动作。
}

/** SSE 试运行：通过 A2A 客户端逐条更新结果面板。 */
async function testRun() {
  if (!trText.value.trim() || saving.value === 'testrun') return
  saving.value = 'testrun'
  trErr.value = ''
  try {
    const previous = trResult.value
    const result = await sendA2aMessage({ slug: detail.value.master.slug, text: trText.value,
      contextId: trContextId.value || undefined,
      taskId: previous?.inputRequired ? previous.taskId : undefined,
      services: JSON.parse(trServices.value),
      onProgress: update => { trResult.value = update }, consolePage: 'multi' })
    trResult.value = result
    trContextId.value = result.contextId
    await historyPager.load()
    await loadRuns()
  } catch (e) {
    if (trResult.value?.taskId && ['SUBMITTED', 'WORKING'].includes(trResult.value.status)) {
      try { trResult.value = await reconnectA2aTask(detail.value.master.slug, trResult.value.taskId, update => { trResult.value = update }, 'multi'); return }
      catch (reconnectError) { trErr.value = `${e.message}；重连失败：${reconnectError.message}` }
    } else trErr.value = e.message
  }
  finally { saving.value = '' }
}
async function stopTestRun() {
  if (!trResult.value?.taskId) return
  try { trResult.value = await cancelA2aTask(detail.value.master.slug, trResult.value.taskId, 'multi') }
  catch (e) { trErr.value = e.message }
}

/**
 * 按 context_id 归类：一个 context 就是一段对话，多轮追问理应挨在一起看。
 *
 * 入参是时间倒序（新的在前），Map 的插入顺序天然让「最近有活动的对话」排在
 * 最前，不用再排一次；组内翻成正序，对话从上往下读。
 */
function groupByContext(entries) {
  const map = new Map()
  for (const h of entries) {
    const cid = h.r.contextId || '(无上下文)'
    if (!map.has(cid)) map.set(cid, [])
    map.get(cid).push(h)
  }
  return [...map.entries()].map(([contextId, turns]) => ({
    contextId,
    turns: [...turns].reverse(),
    latestAt: turns[0]?.r.startedAt || null,
  }))
}

const trGroups = computed(() => groupByContext(trHistory.value))

/** 运行记录 → 与试运行同构的条目，好复用同一套分组和结果面板。 */
const runEntries = computed(() => runs.value.map(row => ({
  q: strippedQuestion(row.input_text), r: runRecordToResult(row),
})))
const runGroups = computed(() => groupByContext(runEntries.value))

// 「加入测试集」联动：在运行监控里发现一条路由错了，直接把它加进评测集，
// 不必回到提示词调优页按"最近 N 条"整批抓——那样会连带抓进一堆无关记录。
const evalDatasets = ref([])
const evalDatasetId = ref('')
async function loadEvalDatasets() {
  try {
    evalDatasets.value = await api.listDatasets(props.masterId)
    if (!evalDatasets.value.some(d => d.dataset_id === evalDatasetId.value)) {
      evalDatasetId.value = evalDatasets.value[0]?.dataset_id || ''
    }
  } catch { evalDatasets.value = [] }
}
async function addRunToDataset(runId) {
  if (!evalDatasetId.value) return
  const resp = await api.appendCases(evalDatasetId.value, [runId])
  success(resp.added
    ? '已加入测试集，去「提示词调优 → 标注工作台」标注'
    : '这条运行记录已经在该测试集里了')
}

/** 按当前分页与筛选条件拉取运行记录。 */
async function loadRuns() { await runsPager.load() }

/** 换筛选条件时回到第一页。 */
async function applyRunFilter() {
  loadEvalDatasets()
  saving.value = 'runs'
  try { await runsPager.load({ reset: true }) } catch { /* toast 已展示 */ } finally { saving.value = '' }
}


/** 点行展开/收起（与试运行同样的折叠语义）。 */
function showRun(entry) {
  runResult.value = (runResult.value && runResult.value.runId === entry.r.runId)
    ? null : entry.r
}

/**
 * 点历史行 → 回看那次的完整结果（答案正文 + 工具返回原文都在库里）。
 * 再点同一行则收起——否则结果面板会一直挂在那儿关不掉。
 */
function showHistory(entry) {
  trResult.value = (trResult.value && trResult.value.runId === entry.r.runId)
    ? null : entry.r
  trErr.value = ''
}

/** 关掉结果面板（实时跑出来的那条也用这个收起）。 */
function closeResult() {
  trResult.value = null
  trErr.value = ''
}

/**
 * 删掉一段对话的运行记录。不可恢复，所以走确认弹窗。
 *
 * 措辞刻意不承诺具体条数：列表只取最近 N 条，被 limit 截断的分组在界面上
 * 看着只有 1 轮、库里可能还有更早的几轮，而后端是按 context_id 删全部。
 * 删完用服务端返回的真实条数报数。
 */
async function deleteContext(group) {
  const n = group.turns.length
  if (!await confirmAction(
    `将删除这段对话的全部运行记录（列表中可见 ${n} 条），不可恢复。`,
    { okText: '删除', danger: true })) return
  saving.value = `delrun:${group.contextId}`
  try {
    const r = await api.deleteMasterRuns(props.masterId, {
      contextId: group.contextId,
    })
    // 正在回看的那条被删掉了就收起，别留个指向已删记录的面板
    if (trResult.value
        && group.turns.some(t => t.r.runId === trResult.value.runId)) {
      trResult.value = null
    }
    if (trContextId.value === group.contextId) trContextId.value = ''
    await loadTestRunHistory()
    await loadRuns()
    success(`已删除 ${r.deleted ?? n} 条记录`)
  } catch { /* toast 已展示 */ } finally {
    saving.value = ''
  }
}

/** 清空该 Team 的全部试运行历史（只清 testrun-，不动 A2A 端点的调用记录）。 */
async function clearTestRunHistory() {
  if (!await confirmAction(
    '将删除这个 Team 的全部试运行记录，不可恢复。'
    + '经 A2A 端点产生的调用记录不受影响。',
    { okText: '全部清空', danger: true })) return
  saving.value = 'delrun:all'
  try {
    const r = await api.deleteMasterRuns(props.masterId, {
      contextPrefix: TESTRUN_CTX_PREFIX,
    })
    trResult.value = null
    trContextId.value = ''
    await loadTestRunHistory()
    await loadRuns()
    success(`已清空 ${r.deleted ?? 0} 条试运行记录`)
  } catch { /* toast 已展示 */ } finally {
    saving.value = ''
  }
}

/** 接着某段对话继续追问：回填该 context，并把最后一轮结果显示出来。 */
function resumeContext(group) {
  trContextId.value = group.contextId
  const newest = group.turns[group.turns.length - 1]
  if (newest) trResult.value = newest.r
  trErr.value = ''
}

/** 清掉上下文，下一句重新开一轮（防止上一轮的澄清残留影响判断）。 */
function newTestContext() {
  trContextId.value = ''
  trResult.value = null
  trErr.value = ''
}

</script>

<template>
  <div>
    <div class="flex items-start justify-between mb-5">
      <div>
        <h1 class="text-xl font-semibold">Agent Team · 详情</h1>
        <p class="text-t2 mt-1">每个 Team 独立管理自己的服务绑定、提示词和推理配置，并拥有一个独立的 A2A 调用地址</p>
      </div>
      <button class="btn btn-ghost" @click="emit('back')">← 返回 Multi-Agent</button>
    </div>

    <div v-if="err" class="card text-brand">{{ err }}</div>

    <!-- 身份栏：只读。原先是个假下拉框（div + ▾，无任何下拉行为），
         Team 的切换在 Agent Team 列表里做，这里固定展示进来的那一个。 -->
    <div v-if="detail" class="bg-white rounded-xl shadow-sm px-5 py-4 mb-5 flex items-center gap-3 flex-wrap">
      <span class="text-base font-semibold">{{ detail.master.display_name || detail.master.slug }}</span>
      <span class="mono text-[12px] bg-canvas text-t2 px-1.5 py-0.5 rounded">{{ detail.master.slug }}</span>
      <span class="badge badge-brand" title="COMPOSITE">Team</span>
      <span :class="detail.master.enabled ? 'badge badge-ok' : 'badge badge-gray'">
        {{ detail.master.enabled ? '已启用' : '已停用' }}
      </span>
      <span class="mono text-[12px] text-t2 ml-auto">A2A 端点: /a2a/{{ detail.master.slug }}/</span>
    </div>
    <p v-if="detail?.master?.description" class="text-t2 text-[13px] -mt-2 mb-5">
      {{ detail.master.description }}
    </p>

    <div class="flex gap-2 mb-5">
      <button v-for="t in detailTabs"
        :key="t[0]" :class="['h-9 px-4 rounded-md text-sm font-medium border transition-colors',
          tab===t[0] ? 'bg-brand text-white border-brand' : 'bg-white border-line hover:border-brand hover:text-brand']"
        @click="tab=t[0]">{{ t[1] }}</button>
    </div>

    <!-- 绑定服务 -->
    <div v-if="tab==='bindings'" class="card">
      <div class="flex items-center justify-between mb-4">
        <div>
          <h3 class="font-semibold">勾选绑定</h3>
          <span class="text-t2 text-[13px]">勾选的服务会变成这个 Team 可以调用的工具。实际运行时还会按调用人的权限再过滤一遍，看不见的服务不会出现。</span>
        </div>
        <div class="flex items-center gap-2">
          <span v-if="bindDirty" class="badge badge-warn">有未保存修改</span>
          <span class="badge badge-ok">已勾选 {{ boundCount }} / 注册 {{ services.length }}</span>
        </div>
      </div>
      <div v-for="s in services" :key="s.service_id"
        class="flex items-center gap-3.5 py-3.5 px-3 border border-line rounded-[10px] mb-2.5 hover:border-brand transition-colors">
        <input type="checkbox" v-model="bindSelection[s.service_id].checked" :disabled="!canManage" class="w-[18px] h-[18px] accent-[#C22128] cursor-pointer" />
        <div class="flex-1 min-w-0">
          <input v-model="bindSelection[s.service_id].toolName" :disabled="!canManage" class="font-semibold text-sm bg-transparent outline-none w-40" />
          <input v-model="bindSelection[s.service_id].toolDescription" :disabled="!canManage" class="block w-full text-xs text-t2 bg-transparent outline-none mt-0.5" />
        </div>
        <select v-if="s.source_type === 'HIAGENT'" v-model="bindSelection[s.service_id].interactionMode"
            :disabled="!canManage" class="input text-xs" style="width: auto"
          title="流程型：总控只选目标，用户原话透传、结果与卡片原样返回，流程未结束的后续轮次直接交给它">
          <option value="TOOL">能力型（模型转述/整理）</option>
          <option value="FLOW">流程型（原话透传/原样返回）</option>
        </select>
        <span class="badge" :class="s.compatibility==='COMPATIBLE' ? 'badge-ok' : 'badge-warn'">{{ s.compatibility }}</span>
        <span class="text-xs text-t2 bg-canvas rounded px-2 py-0.5 font-mono">tool: {{ bindSelection[s.service_id].toolName }}</span>
      </div>
      <div class="mt-4 flex items-center gap-3">
        <button v-if="canManage" class="btn btn-primary" :disabled="saving === 'bindings' || !bindDirty"
                @click="saveBindings">
          <svg v-if="saving === 'bindings'" class="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M21 12a9 9 0 11-2.64-6.36"/></svg>
          {{ saving === 'bindings' ? '保存中…' : '保存绑定' }}
        </button>
        <span v-if="!bindDirty" class="text-[13px] text-t3">当前配置与已保存内容一致</span>
      </div>
    </div>

    <!-- 知识库 -->
    <div v-if="tab==='kb'" class="card">
      <div class="flex items-center justify-between mb-4">
        <div>
          <h3 class="font-semibold">可检索的知识库</h3>
          <span class="text-t2 text-[13px]">勾选的知识库会变成这个 Team 的只读检索工具。<span class="text-t3">保存后立即生效，无需重启；Agent 只能查，不能改知识库内容。</span></span>
        </div>
        <div class="flex items-center gap-2">
          <span v-if="kbDirty" class="badge badge-warn">有未保存修改</span>
          <span class="badge badge-ok">已勾选 {{ kbBoundCount }} / 共 {{ kbs.length }}</span>
        </div>
      </div>

      <div v-if="!kbs.length" class="text-center text-t3 py-8 text-sm">
        还没有任何知识库。先去左侧「知识库」建一个并完成编译。
      </div>

      <div v-for="k in kbs" :key="k.kb_id"
        class="flex items-center gap-3.5 py-3.5 px-3 border border-line rounded-[10px] mb-2.5 transition-colors"
        :class="k.status === 'READY' ? 'hover:border-brand' : 'opacity-60'">
        <input type="checkbox" v-model="kbSelection[k.kb_id]" :disabled="!canManage || k.status !== 'READY'"
               class="w-[18px] h-[18px] accent-[#C22128] cursor-pointer disabled:cursor-not-allowed" />
        <div class="flex-1 min-w-0">
          <div class="font-semibold text-sm">{{ k.display_name }}</div>
          <div class="text-xs text-t2 mt-0.5">
            {{ k.source_count }} 份原始文档 · {{ k.page_count }} 个页面 · 版本 {{ k.current_rev || '尚未编译' }}
          </div>
        </div>
        <span class="badge" :class="k.status === 'READY' ? 'badge-ok' : 'badge-gray'" :title="k.status">
          {{ k.status === 'READY' ? '可用' : '尚不可用' }}
        </span>
        <span class="text-xs text-t2 bg-canvas rounded px-2 py-0.5 font-mono">tool: kb_search_{{ k.slug }}</span>
      </div>

      <div class="mt-4 flex items-center gap-3">
        <button v-if="canManage" class="btn btn-primary" :disabled="saving === 'kb' || !kbDirty" @click="saveKbBindings">
          <svg v-if="saving === 'kb'" class="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M21 12a9 9 0 11-2.64-6.36"/></svg>
          {{ saving === 'kb' ? '保存中…' : '保存知识库绑定' }}
        </button>
        <span v-if="!kbDirty" class="text-[13px] text-t3">当前配置与已保存内容一致</span>
      </div>
    </div>

    <!-- 提示词版本 -->
    <div v-if="tab==='prompts'">
      <div class="card">
        <div class="flex items-center justify-between mb-4">
          <h3 class="font-semibold">Team 编排提示词
            <span class="text-t2 text-[13px] font-normal">（提示词存在数据库里并带版本，发布后立即生效，无需重启）</span>
          </h3>
        </div>
        <textarea v-model="promptDraft" :disabled="!canManage" class="input min-h-64 leading-7 font-mono"></textarea>
        <div class="flex gap-2 mt-4">
          <button v-if="canManage" class="btn btn-primary" :disabled="saving === 'prompt' || !promptDraft.trim()"
                  @click="savePrompt">
            <svg v-if="saving === 'prompt'" class="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M21 12a9 9 0 11-2.64-6.36"/></svg>
            {{ saving === 'prompt' ? '保存中…' : '保存为新版本 Draft' }}
          </button>
        </div>
      </div>
      <div class="card !p-0 overflow-hidden">
        <table class="tbl">
          <thead><tr><th>版本</th><th>状态</th><th>怎么来的</th><th>创建人</th><th>时间</th><th></th></tr></thead>
          <tbody>
            <tr v-for="p in prompts" :key="p.prompt_version_id">
              <td class="font-semibold">v{{ p.version_no }}</td>
              <td><span :class="{'PUBLISHED':'badge badge-ok','DRAFT':'badge badge-warn','SUPERSEDED':'badge badge-gray'}[p.status]">{{ p.status }}</span></td>
              <td class="mono">{{ p.source }}</td>
              <td class="mono">{{ p.created_by }}</td>
              <td class="mono">{{ fmtDateTime(p.created_at, '') }}</td>
              <td>
                <button v-if="canManage && p.status!=='PUBLISHED'" class="btn btn-text"
                        :disabled="saving === `publish:${p.prompt_version_id}`"
                        @click="publish(p.prompt_version_id)">
                  {{ saving === `publish:${p.prompt_version_id}` ? '发布中…' : '发布/回滚到此版' }}
                </button>
                <span v-else class="text-t3 text-xs">当前生效</span>
              </td>
            </tr>
          </tbody>
        </table>
        <Pagination :pager="promptsPager" />
      </div>
    </div>

    <!-- 循环配置 -->
    <div v-if="tab==='loop'" class="card">
      <h3 class="font-semibold mb-1">循环配置</h3>
      <p class="text-t2 text-[13px] mb-4">Team 不是「一次性分类器」，而是边想边做的循环：每一轮它决定调哪个服务、看完结果再决定下一步。下面这个数字限制单次回复内最多循环几轮。</p>
      <div class="max-w-md space-y-4">
        <div>
          <label class="block text-[13px] text-t2 mb-1.5">最大推理轮数（多数场景一轮就够，建议填 3–10；调太大会变慢且更费 token）</label>
          <input type="number" v-model.number="maxIters" min="1" max="50" :disabled="!canManage" class="input" />
        </div>
        <div>
          <label class="block text-[13px] text-t2 mb-1.5">响应模式</label>
          <select v-model="responseMode" :disabled="!canManage" class="input">
            <option value="SYNTHESIZE">模型整理</option>
            <option value="SINGLE_TOOL_RAW">单工具原样返回</option>
          </select>
          <p class="text-t3 text-xs mt-1.5">
            原样模式仅在本轮恰好一次成功的文本工具调用、且无需追问时生效；其他情况仍由模型整理。
          </p>
        </div>
      </div>
      <button v-if="canManage" class="btn btn-primary mt-4" :disabled="saving === 'loop'" @click="saveIterConfig">
        <svg v-if="saving === 'loop'" class="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M21 12a9 9 0 11-2.64-6.36"/></svg>
        {{ saving === 'loop' ? '保存中…' : '保存' }}
      </button>
    </div>

    <!-- 模型配置 -->
    <div v-if="tab==='model'" class="card">
      <h3 class="font-semibold mb-1">模型配置</h3>
      <p class="text-t2 text-[13px] mb-4">
        每个 Agent Team 可独立指定 OpenAI 兼容模型（Base URL + API Key + 模型名），脱钩平台单一模型；
        留空则使用平台全局默认模型；这里填了就只对本 Team 生效，且优先级高于全局。
      </p>
      <div class="max-w-md space-y-3">
        <div>
          <label class="block text-[13px] text-t2 mb-1.5">接口地址（需兼容 OpenAI 协议，如 https://api.deepseek.com/v1）</label>
          <input v-model="modelCfg.baseUrl" :disabled="!canManage" class="input" placeholder="https://api.example.com/v1" />
        </div>
        <div>
          <label class="block text-[13px] text-t2 mb-1.5">模型 ID（发给接口的那个名字，如 deepseek-chat）</label>
          <input v-model="modelCfg.model" :disabled="!canManage" class="input" placeholder="glm-4.7 / deepseek-chat / ..." />
        </div>
        <div>
          <label class="block text-[13px] text-t2 mb-1.5">API Key</label>
          <input v-model="modelCfg.apiKey" type="password" :disabled="!canManage" class="input"
            :placeholder="modelCfgSet ? '已设置（留空保持不变）' : '输入 API Key'" />
          <span v-if="modelCfgSet" class="text-t3 text-xs">
            当前已配置（尾 4 位：{{ detail?.master?.model_config?.api_key_masked }}）
          </span>
        </div>
      </div>
      <div class="flex gap-2 mt-4">
        <button v-if="canManage" class="btn btn-primary" :disabled="saving === 'model'" @click="saveModelConfig">
          <svg v-if="saving === 'model'" class="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M21 12a9 9 0 11-2.64-6.36"/></svg>
          {{ saving === 'model' ? '保存中…' : '保存' }}
        </button>
        <button v-if="canManage && modelCfgSet" class="btn btn-ghost" :disabled="saving === 'model'"
                @click="clearModelConfig">恢复平台默认</button>
      </div>
    </div>

    <!-- 权限 -->
    <div v-if="tab==='perm'" class="card">
      <h3 class="font-semibold mb-1">权限</h3>
      <p class="text-t2 text-[13px] leading-7">
        运行时身份取自请求头 <code class="mono">cap-user-name</code>（工号）。可见集合 = 组可见 ∪ 个人直授；
        Agent Team 的 Toolkit 按该集合裁剪——用户不可见的服务，模型既看不到也无法调用。
        前往「治理 → 权限管理」维护映射关系。
      </p>
    </div>

    <!-- 试运行 -->
    <div v-if="tab==='testrun' && canManage">
      <div class="card">
        <div class="flex items-center justify-between mb-1">
          <h3 class="font-semibold">发一句话试试</h3>
          <span class="mono text-[12px] text-t2">等价于调用 /a2a/{{ detail?.master?.slug }}/</span>
        </div>
        <p class="text-t2 text-[13px] mb-4">
          走的是和线上完全相同的执行链路（同一个 run_master），会真实调用模型和后端服务，
          并落一条运行记录。用来验证意图路由和答案质量，不是压测。
        </p>

        <textarea v-model="trText" class="input min-h-28 leading-7"
          placeholder="例如：北京明天天气怎么样？"
          @keydown.ctrl.enter="testRun" @keydown.meta.enter="testRun"></textarea>

        <div class="grid grid-cols-1 md:grid-cols-2 gap-4 mt-4">
          <div>
            <label class="block text-[13px] text-t2 mb-1.5">当前登录身份</label>
            <input :value="getSession()?.userId" class="input mono" disabled />
            <p class="text-t3 text-[12px] mt-1.5">
              A2A 服务工具受可见性约束，换个没授权的工号，工具箱里就没有它们；
              知识库工具不受此约束（绑定即授权）。
            </p>
          </div>
          <div>
            <label class="block text-[13px] text-t2 mb-1.5">对话上下文</label>
            <div class="flex gap-2">
              <input v-model="trContextId" class="input mono" placeholder="留空 = 每次新开一轮" />
              <button class="btn btn-ghost shrink-0" @click="newTestContext">新开一轮</button>
            </div>
            <p class="text-t3 text-[12px] mt-1.5">
              回答后会自动回填。想接着追问就直接发下一句；换个话题记得先「新开一轮」。
            </p>
          </div>
        </div>

        <div class="flex items-center gap-4 mt-4 flex-wrap">
          <button class="btn btn-primary" :disabled="!trText.trim() || saving==='testrun'"
            @click="testRun">
            {{ saving === 'testrun' ? '执行中…' : '发送' }}
          </button>
          <button v-if="saving==='testrun' && trResult?.taskId" class="btn btn-ghost" @click="stopTestRun">停止任务</button>
          <span class="text-t3 text-[12px]">Ctrl / ⌘ + Enter</span>
        </div>

        <label class="block mt-4 text-[13px]">服务参数（按服务 slug 分组）</label>
        <textarea v-model="trServices" class="input w-full mono" rows="3" />
        <p v-if="trErr" class="text-brand text-[13px] mt-4">{{ trErr }}</p>
      </div>

      <!-- 结果（与运行监控共用同一个面板组件） -->
      <RunResultPanel v-if="trResult" :result="trResult" :tool-labels="runToolLabels"
        @close="closeResult" @open-trace="openTrace" />

      <!-- 试运行历史：来自服务端 run_record，切页面/换机器都还在 -->
      <div class="card">
        <div class="flex items-baseline justify-between gap-3 flex-wrap mb-1">
          <h3 class="font-semibold">试运行历史</h3>
          <div class="flex items-baseline gap-3 ml-auto">
            <span class="text-t3 text-[12px]">
              存在服务端运行记录里（共 {{ historyPager.total }} 次调用，
              归为 {{ trGroups.length }} 段对话）
            </span>
            <button v-if="canManage" class="btn btn-text" :disabled="saving === 'delrun:all'"
              @click="clearTestRunHistory">全部清空</button>
          </div>
        </div>
        <p class="text-t2 text-[13px] mb-4">
          同一段对话里的多轮追问共享上下文，模型看得到前几轮，所以放在一起看。
          点任意一行展开/收起当时的完整结果。
        </p>

        <RunHistoryGroups :groups="trGroups" :selected-run="trResult" :tool-labels="runToolLabels"
          @select="showHistory">
          <template #group-actions="{ group }">
            <button class="btn btn-text ml-auto"
              :class="trContextId === group.contextId ? 'font-semibold' : ''"
              @click="resumeContext(group)">
              {{ trContextId === group.contextId ? '✓ 当前上下文' : '接着这段问' }}
            </button>
            <button v-if="canManage" class="btn btn-text text-t3 hover:text-brand"
              :disabled="saving === `delrun:${group.contextId}`"
              :title="`删除整段对话（当前页显示 ${group.turns.length} 条，含其他页记录）`"
              @click="deleteContext(group)">删除</button>
          </template>
        </RunHistoryGroups>
        <Pagination :pager="historyPager" />
        <p class="text-xs text-t3">按运行记录分页，同一上下文可能跨页展示。</p>
      </div>
    </div>

    <!-- 运行监控：全部来源的调用记录，排障入口 -->
    <div v-if="tab==='runs'">
      <div class="card">
        <div class="flex items-baseline justify-between gap-3 flex-wrap mb-1">
          <h3 class="font-semibold">运行监控</h3>
          <span class="text-t3 text-[12px]">
            已加载 {{ runs.length }} 条，归为 {{ runGroups.length }} 段对话
          </span>
        </div>
        <p class="text-t2 text-[13px] mb-4">
          这个 Team 被调用的全部记录，不只是管理台里点出来的。
          业务方经 A2A 端点打进来的调用只会出现在这里。点任意一行展开/收起完整结果。
        </p>

        <div class="flex items-center gap-4 flex-wrap">
          <div class="flex items-center gap-2">
            <span class="text-[13px] text-t2">来源</span>
            <select v-model="runSource" class="input !w-auto" @change="applyRunFilter">
              <option v-for="s in RUN_SOURCES" :key="s[0]" :value="s[0]">{{ s[1] }}</option>
            </select>
          </div>
          <label class="flex items-center gap-2 text-[13px] cursor-pointer">
            <input type="checkbox" v-model="runOnlyProblems" @change="applyRunFilter" />
            只看异常
            <span class="text-t3">（失败 / 达到轮数上限 / 被拒绝 / 已取消）</span>
          </label>
          <span class="ml-auto flex items-center gap-2">
            <span class="text-[12px] text-t3">加入测试集：</span>
            <select v-model="evalDatasetId" class="input !w-52 !h-8 text-[13px]">
              <option value="">（先在提示词调优页建一个）</option>
              <option v-for="d in evalDatasets" :key="d.dataset_id" :value="d.dataset_id">
                {{ d.name }} v{{ d.version_no }}
              </option>
            </select>
          </span>
          <button class="btn btn-ghost" :disabled="saving === 'runs'"
            @click="applyRunFilter">{{ saving === 'runs' ? '刷新中…' : '刷新' }}</button>
        </div>
      </div>

      <RunResultPanel v-if="runResult" :result="runResult" :tool-labels="runToolLabels"
        @close="runResult = null" @open-trace="openTrace" />

      <div v-if="!runs.length" class="card text-t3 text-[13px]">
        <template v-if="runOnlyProblems || runSource !== 'all'">
          当前筛选条件下没有记录。换个来源或去掉「只看异常」再看看。
        </template>
        <template v-else>
          还没有运行记录。通过这个 Team 的 A2A 地址发起一次调用，或到「试运行」页发一句话。
        </template>
      </div>

      <div v-else class="card">
        <RunHistoryGroups mode="runs" :groups="runGroups" :selected-run="runResult" :tool-labels="runToolLabels"
          @select="showRun">
          <template #row-actions="{ entry }">
            <button v-if="canManage && canLoopManage" class="btn btn-text whitespace-nowrap" :disabled="!evalDatasetId"
              :title="evalDatasetId ? '把这条运行记录加进上方选中的测试集' : '先在提示词调优页建一个测试集'"
              @click="addRunToDataset(entry.r.runId)">加入</button>
          </template>
        </RunHistoryGroups>

        <Pagination :pager="runsPager" />
        <p class="text-xs text-t3">按运行记录分页，同一上下文可能跨页展示。</p>
      </div>
    </div>
  </div>
</template>
