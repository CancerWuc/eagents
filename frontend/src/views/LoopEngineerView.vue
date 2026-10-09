<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { api } from '../api.js'
import { fmtDateTime } from '../format.js'
import { confirmAction, promptText } from '../ui.js'
import { success } from '../notify.js'

// 跨页联动：标注时要能跳到原始调用链看"到底调了什么"
const emit = defineEmits(['open-trace'])
defineProps({ canManage: { type: Boolean, default: false } })

import Pagination from '../components/Pagination.vue'
import { useServerPagination } from '../pagination.js'

const err = ref('')
const subTab = ref('datasets')

// master 选择：以前这里写死 masters[0]，而 master 列表按 created_at DESC 排，
// 新建一个 Team 就会把默认调优对象换掉，用户完全无感知。现在显式选并记住。
const masters = ref([])
const masterId = ref('')
const currentMaster = computed(
  () => masters.value.find(m => m.master_id === masterId.value) || null)

const datasets = ref([])
const activeDataset = ref(null)
const cases = ref([])
const toolNames = ref([])

const annotation = ref({ caseId: null, input: '', correct: true, corrected: '', note: '', expectedText: '' })

// 优化任务：不再靠组件内存里的 id 记住任务，刷新/切页都能找回来
const optimizations = ref([])
const activeOpt = ref(null)
const busy = ref(false)
const reviewBusy = ref(false)
const reviewMsg = ref('')
const currentPromptText = ref('')

const datasetsPager = useServerPagination(datasets, () => `/api/v1/datasets?master_id=${masterId.value}`)
const casesPager = useServerPagination(cases, () => `/api/v1/datasets/${activeDataset.value?.dataset_id}/cases`)
const optimizationsPager = useServerPagination(optimizations, () => `/api/v1/optimizations?master_id=${masterId.value}`, {
  onResult: () => { activeOpt.value = null; currentPromptText.value = '' },
})

async function loadMasters() {
  try {
    const catalog = await api.agentSelector('loop')
    masters.value = catalog.items.map(m => ({ master_id: m.masterId, slug: m.slug, display_name: m.displayName }))
    if (!masterId.value && masters.value.length) {
      const remembered = localStorage.getItem('eagent-loop-master')
      masterId.value = masters.value.some(m => m.master_id === remembered)
        ? remembered : masters.value[0].master_id
    }
    err.value = ''
  } catch (e) { err.value = e.message }
}
onMounted(async () => { await loadMasters(); await reloadForMaster() })

watch(masterId, async (id) => {
  if (!id) return
  datasetsPager.invalidate(); casesPager.invalidate(); optimizationsPager.invalidate()
  localStorage.setItem('eagent-loop-master', id)
  activeDataset.value = null
  cases.value = []
  activeOpt.value = null
  await reloadForMaster()
})

async function reloadForMaster() {
  if (!masterId.value) return
  const id = masterId.value
  try {
    const [, , context] = await Promise.all([
      datasetsPager.load(),
      optimizationsPager.load(),
      api.agentLoopContext(id),
    ])
    if (masterId.value === id) toolNames.value = context.toolNames || []
    err.value = ''
  } catch (e) { err.value = e.message }
}

// ---------------------------------------------------------------- 测试集
async function createDataset() {
  if (!masterId.value) return
  const name = await promptText('测试集名称',
    `意图识别-${new Date().toISOString().slice(0, 10)}`)
  if (!name) return
  await api.createDataset({ name: name.trim(), masterId: masterId.value, runIds: [] })
  await reloadForMaster()
}
async function renameDataset(d) {
  const name = await promptText('新的测试集名称', d.name)
  if (!name || name.trim() === d.name) return
  await api.renameDataset(d.dataset_id, name.trim())
  await reloadForMaster()
}
async function removeDataset(d) {
  const ok = await confirmAction(
    `删除测试集「${d.name}」？集内用例与标注会一并删除，已产生的优化记录保留可追溯。`,
    { okText: '删除', danger: true })
  if (!ok) return
  await api.deleteDataset(d.dataset_id)
  if (activeDataset.value?.dataset_id === d.dataset_id) {
    activeDataset.value = null
    cases.value = []
  }
  await reloadForMaster()
}
async function openDataset(d) {
  activeDataset.value = d
  await casesPager.load({ reset: true })
  subTab.value = 'cases'
}

// ---------------------------------------------------------------- 标注
function startAnnotate(c) {
  annotation.value = {
    caseId: c.case_id,
    input: c.input_text,
    correct: c.annotations?.[0] ? !!c.annotations[0].intent_correct : true,
    corrected: '',
    note: '',
    expectedText: '',
  }
}
async function submitAnnotation() {
  if (!annotation.value.caseId) return
  const body = {
    intentCorrect: annotation.value.correct,
    subjectiveNote: annotation.value.note,
  }
  // 只有判"错误"时才谈得上纠正正确答案
  if (!annotation.value.correct && annotation.value.corrected) {
    body.correctedService = annotation.value.corrected
  }
  if (annotation.value.expectedText.trim()) {
    body.expectedText = annotation.value.expectedText
  }
  const resp = await api.annotate(annotation.value.caseId, body)
  success(resp.expected_service_updated || resp.expected_text_updated
    ? '标注已提交，期望值已更新（文本指标将参与下一轮评分）'
    : '标注已提交')
  annotation.value = { caseId: null, input: '', correct: true, corrected: '', note: '', expectedText: '' }
  if (activeDataset.value) {
    await casesPager.load()
  }
}
function viewCaseTrace(c) {
  if (!c.source_trace_id) return
  emit('open-trace', { traceId: c.source_trace_id, returnLabel: '返回提示词调优' })
}
// 标了"错误"却没给正确答案的用例会被优化排除在评分之外，提前提示
const pendingCorrection = computed(() => cases.value.filter(
  c => c.annotations?.[0] && !c.annotations[0].intent_correct
    && !c.annotations[0].corrected_service).length)

// ---------------------------------------------------------------- 优化
async function triggerOptimize() {
  if (!activeDataset.value) return
  busy.value = true
  try {
    const resp = await api.createOptimization({
      masterId: masterId.value,
      datasetId: activeDataset.value.dataset_id,
    })
    success('优化任务已发起，可以离开本页，稍后回「优化任务」查看')
    await reloadForMaster()
    subTab.value = 'runs'
    await openOptimization({ optimization_id: resp.optimization_id })
  } finally { busy.value = false }
}
async function openOptimization(row) {
  activeOpt.value = await api.getOptimization(row.optimization_id)
  reviewMsg.value = ''
  await loadCurrentPrompt()
  if (activeOpt.value.candidates?.length) subTab.value = 'compare'
}
async function refreshOptimizations() { await reloadForMaster() }

async function loadCurrentPrompt() {
  const opt = activeOpt.value?.optimization
  if (!opt?.master_id) return
  try {
    const context = await api.agentLoopContext(opt.master_id)
    currentPromptText.value = context.currentPrompt?.prompt_text || ''
  } catch { currentPromptText.value = '' }
}

const candidate = computed(() => activeOpt.value?.candidates?.[0] || null)
// metric_json / react_config_json 是 JSON 列，接口原样回字符串（与 master 的
// react_config_json 同约定）。此前模板直接对字符串取 .baseline，恒为
// undefined —— 两个指标框一直是空的。统一在这里解析一次。
function parseJson(v) {
  if (!v) return null
  if (typeof v === 'object') return v
  try { return JSON.parse(v) } catch { return null }
}
const metrics = computed(() => parseJson(candidate.value?.metric_json))
const annotationInfo = computed(() => metrics.value?.annotation || null)
const failureCategories = computed(() => metrics.value?.failure_categories || null)

// 行级 LCS diff: left=当前线上版本, right=优化候选稿
const diffRows = computed(() => {
  const a = (currentPromptText.value || '').split('\n')
  const b = (candidate.value?.prompt_text || '').split('\n')
  const n = a.length, m = b.length
  const dp = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0))
  for (let i = n - 1; i >= 0; i--)
    for (let j = m - 1; j >= 0; j--)
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1])
  const out = []
  let i = 0, j = 0
  while (i < n && j < m) {
    if (a[i] === b[j]) { out.push({ l: a[i], r: b[j], kind: 'same' }); i++; j++ }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { out.push({ l: a[i], r: '', kind: 'del' }); i++ }
    else { out.push({ l: '', r: b[j], kind: 'add' }); j++ }
  }
  while (i < n) out.push({ l: a[i++], r: '', kind: 'del' })
  while (j < m) out.push({ l: '', r: b[j++], kind: 'add' })
  return out
})

async function approveCandidate() {
  const cand = candidate.value, opt = activeOpt.value?.optimization
  if (!cand || !opt) return
  const ok = await confirmAction(
    '确认发布这份候选提示词？发布后下一次调用即生效，当前线上版本会被它取代。',
    { okText: '发布' })
  if (!ok) return
  reviewBusy.value = true
  try {
    await api.approveCandidate(opt.optimization_id, cand.candidate_id)
    reviewMsg.value = '已发布（hot update），下一次 run 生效'
    await loadCurrentPrompt()
  } catch (e) { reviewMsg.value = `发布失败: ${e.message}` }
  finally { reviewBusy.value = false }
}
async function rejectCandidate() {
  const cand = candidate.value, opt = activeOpt.value?.optimization
  if (!cand || !opt) return
  reviewBusy.value = true
  try {
    await api.rejectCandidate(opt.optimization_id, cand.candidate_id)
    cand.recommendation = 'REJECTED'
    reviewMsg.value = '已驳回（DRAFT 版本保留，可回溯）'
  } catch (e) { reviewMsg.value = `驳回失败: ${e.message}` }
  finally { reviewBusy.value = false }
}

const STATUS_TEXT = { RUNNING: '进行中', COMPLETED: '已完成', FAILED: '失败' }
const statusClass = (s) => ({
  RUNNING: 'badge badge-warn', COMPLETED: 'badge badge-ok', FAILED: 'badge badge-err',
}[s] || 'badge badge-gray')
</script>

<template>
  <div>
    <div class="mb-5">
      <h1 class="text-xl font-semibold">提示词调优</h1>
      <p class="text-t2 mt-1">拿真实调用记录攒一批测试用例 → 你标注对错并给出正确答案 → 系统据此改进提示词。<span class="text-t3">改出来的只是候选稿，必须你审核通过才会上线。</span></p>
    </div>

    <div v-if="err" class="card text-brand mb-4">{{ err }}</div>

    <div class="card mb-5 flex items-center gap-3">
      <span class="text-sm text-t2">调优对象</span>
      <select v-model="masterId" class="input !w-80">
        <option v-for="m in masters" :key="m.master_id" :value="m.master_id">
          {{ m.display_name }}（标识：{{ m.slug }}）
        </option>
      </select>
      <span v-if="currentMaster" class="text-xs text-t3">
        可用工具：{{ toolNames.join('、') || '（未绑定服务）' }}
      </span>
    </div>

    <div class="flex gap-2 mb-5">
      <button v-for="t in [['datasets','测试集'],['cases','标注工作台'],['runs','优化任务'],['compare','优化对比']]"
        :key="t[0]" :class="['h-9 px-4 rounded-md text-sm font-medium border transition-colors',
          subTab===t[0] ? 'bg-brand text-white border-brand' : 'bg-white border-line hover:border-brand hover:text-brand']"
        @click="subTab=t[0]">{{ t[1] }}</button>
      <div class="flex-1"></div>
      <button v-if="canManage" class="btn btn-primary" :disabled="busy || !activeDataset" @click="triggerOptimize">
        {{ busy ? '发起中…' : '触发优化' }}
      </button>
    </div>

    <!-- 测试集 -->
    <div v-if="subTab==='datasets'" class="card !p-0 overflow-hidden">
      <div class="flex items-center justify-between px-6 pt-5">
        <h3 class="font-semibold">测试集 <span class="text-t2 text-[13px] font-normal">（从该 Team 的真实运行记录抽样生成，带版本）</span></h3>
        <button v-if="canManage" class="btn btn-primary btn-sm" :disabled="!masterId" @click="createDataset">从运行记录生成测试集</button>
      </div>
      <table class="tbl mt-2">
        <thead><tr><th>名称</th><th>版本</th><th>状态</th><th>创建时间</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="d in datasets" :key="d.dataset_id"
            :class="activeDataset?.dataset_id === d.dataset_id ? 'bg-brand-lighter' : ''">
            <td class="font-semibold">{{ d.name }}</td>
            <td class="mono">v{{ d.version_no }}</td>
            <td><span class="badge badge-ok">{{ d.status }}</span></td>
            <td class="mono">{{ fmtDateTime(d.created_at, '') }}</td>
            <td>
              <div class="flex gap-1.5">
                <button class="btn btn-text" @click="openDataset(d)">打开用例</button>
                <button v-if="canManage" class="btn btn-text" @click="renameDataset(d)">改名</button>
                <button v-if="canManage" class="btn btn-text text-red-600" @click="removeDataset(d)">删除</button>
              </div>
            </td>
          </tr>
          <tr v-if="!datasets.length"><td colspan="5" class="text-t3">该 Team 还没有测试集。需要先有真实运行记录才能抽样构建；也可以在「调用链追踪」里挑单条加入已有测试集</td></tr>
        </tbody>
      </table>
      <Pagination :pager="datasetsPager" />
    </div>

    <!-- 标注工作台 -->
    <div v-if="subTab==='cases'">
      <div v-if="pendingCorrection" class="card mb-4 text-[13px] leading-6">
        有 <b>{{ pendingCorrection }}</b> 条用例被标成「错误」但没给出正确答案。
        这类用例的标准答案存疑，<b>优化时会被排除在评分之外</b>——按原期望值打分会把正确的候选判成错。
        请补上「正确答案应该是哪个服务」。
      </div>
      <div class="card !p-0 overflow-hidden">
        <div class="px-6 pt-5">
          <h3 class="font-semibold">用例 <span class="text-t2 text-[13px] font-normal">{{ activeDataset ? '· ' + activeDataset.name : '（未选择测试集）' }}</span></h3>
        </div>
        <table class="tbl mt-2">
          <thead><tr><th>用户原话（已脱敏）</th><th>正确答案应该是哪个服务</th><th>标注</th><th>操作</th></tr></thead>
          <tbody>
            <tr v-for="c in cases" :key="c.case_id">
              <td class="max-w-120 truncate">{{ c.input_text }}</td>
              <td>
                <span class="badge badge-gray">{{ c.expected_service || '—' }}</span>
                <span v-if="c.annotations?.[0]?.corrected_service" class="text-[11px] text-t3 ml-1">已人工纠正</span>
              </td>
              <td>
                <span v-if="c.annotations?.length"
                  :class="c.annotations[0].intent_correct ? 'badge badge-ok' : 'badge badge-err'">
                  {{ c.annotations[0].intent_correct ? '✓ 正确' : '✗ 错误' }}
                </span>
                <span v-else class="badge badge-warn">待你标注</span>
              </td>
              <td>
                <div class="flex gap-1.5">
                  <button v-if="canManage" class="btn btn-text" @click="startAnnotate(c)">标注</button>
                  <button class="btn btn-text" :disabled="!c.source_trace_id"
                    :title="c.source_trace_id ? '查看这条用例的原始调用链' : '该用例没有关联的运行记录'"
                    @click="viewCaseTrace(c)">查看调用链</button>
                </div>
              </td>
            </tr>
            <tr v-if="!cases.length"><td colspan="4" class="text-t3">没有用例</td></tr>
          </tbody>
        </table>
      <Pagination :pager="casesPager" />
      </div>

      <div class="card mt-5">
        <h3 class="font-semibold mb-4">标注</h3>
        <div v-if="annotation.caseId" class="bg-canvas rounded-lg p-4 mb-4 text-sm leading-7">
          用户消息：<b>{{ annotation.input }}</b>
        </div>
        <div v-else class="text-t3 text-[13px] mb-4">在上面的用例列表里点「标注」选择一条。</div>
        <div class="mb-4">
          <label class="block text-[13px] text-t2 mb-1.5">这次的意图识别对不对？</label>
          <div class="flex gap-2">
            <button :class="['h-9 px-4 rounded-md text-sm font-medium border flex-1 justify-center flex',
              annotation.correct ? 'bg-brand text-white border-brand' : 'bg-white border-line']" @click="annotation.correct=true">✓ 正确</button>
            <button :class="['h-9 px-4 rounded-md text-sm font-medium border flex-1 justify-center flex',
              !annotation.correct ? 'bg-brand text-white border-brand' : 'bg-white border-line']" @click="annotation.correct=false">✗ 错误</button>
          </div>
        </div>
        <div v-if="!annotation.correct" class="mb-4">
          <label class="block text-[13px] text-t2 mb-1.5">
            正确答案应该是哪个服务？
            <span class="text-t3">（不填的话这条用例会被优化排除在评分之外）</span>
          </label>
          <select v-model="annotation.corrected" class="input !w-72">
            <option value="">— 我也不确定 —</option>
            <option v-for="t in toolNames" :key="t" :value="t">{{ t }}</option>
          </select>
        </div>
        <div class="mb-4">
          <label class="block text-[13px] text-t2 mb-1.5">
            标准答案文本（可选）
            <span class="text-t3">（填写后回放将计算 BLEU / ROUGE / F1 五项文本相似度）</span>
          </label>
          <textarea v-model="annotation.expectedText" class="input min-h-16 leading-6" placeholder="例如：上海住宿上限为 G6 级每晚 700 元"></textarea>
        </div>
        <div class="mb-4">
          <label class="block text-[13px] text-t2 mb-1.5">补充说明（可选）</label>
          <textarea v-model="annotation.note" class="input min-h-24 leading-6" placeholder="例如：这类问题应该走天气服务；建议提示词补充映射示例"></textarea>
        </div>
        <button v-if="canManage" class="btn btn-primary" :disabled="!annotation.caseId" @click="submitAnnotation">提交标注</button>
      </div>
    </div>

    <!-- 优化任务 -->
    <div v-if="subTab==='runs'" class="card !p-0 overflow-hidden">
      <div class="flex items-center justify-between px-6 pt-5">
        <h3 class="font-semibold">优化任务
          <span class="text-t2 text-[13px] font-normal">（回放要跑几分钟，发起后可以离开本页，回来再看）</span>
        </h3>
        <button class="btn btn-sm" @click="refreshOptimizations">刷新</button>
      </div>
      <table class="tbl mt-2">
        <thead><tr><th>任务</th><th>测试集</th><th>状态</th><th>结论</th><th>发起时间</th><th>结束时间</th><th></th></tr></thead>
        <tbody>
          <tr v-for="o in optimizations" :key="o.optimization_id">
            <td class="mono text-[12px]">{{ o.optimization_id }}</td>
            <td>{{ o.dataset_name || '—' }}</td>
            <td><span :class="statusClass(o.status)">{{ STATUS_TEXT[o.status] || o.status }}</span></td>
            <td>
              <span v-if="o.recommendation === 'PROPOSE'" class="badge badge-ok">建议采纳</span>
              <span v-else-if="o.recommendation" class="badge badge-gray">{{ o.recommendation }}</span>
              <span v-else class="text-t3">—</span>
            </td>
            <td class="mono text-[12px]">{{ fmtDateTime(o.created_at, '') }}</td>
            <td class="mono text-[12px]">{{ fmtDateTime(o.finished_at, '') || '—' }}</td>
            <td><button class="btn btn-text" :disabled="o.status === 'RUNNING'" @click="openOptimization(o)">查看对比</button></td>
          </tr>
          <tr v-if="!optimizations.length"><td colspan="7" class="text-t3">还没有优化任务。打开一个测试集后点右上角「触发优化」</td></tr>
        </tbody>
      </table>
      <Pagination :pager="optimizationsPager" />
    </div>

    <!-- 优化对比 -->
    <div v-if="subTab==='compare'" class="card">
      <h3 class="font-semibold mb-4">优化候选对比</h3>
      <div v-if="!activeOpt" class="text-t3">还没有选中的优化结果。到「优化任务」里点「查看对比」。<span class="text-t3">优化过程用模拟响应回放，不会真的调用外部服务；产出的只是候选稿。</span></div>
      <div v-else-if="activeOpt.optimization.status === 'FAILED'" class="text-brand text-[13px] mono">
        任务失败：{{ activeOpt.optimization.report_json }}
      </div>
      <div v-else-if="!candidate" class="text-t3">这次优化没有产出候选（可能未达到最小提升边际）。结论：{{ activeOpt.optimization.report_json }}</div>
      <div v-else>
        <div v-if="annotationInfo" class="mb-4 text-[13px] text-t2 leading-6">
          本次参与评分 <b>{{ annotationInfo.scored }}</b> 条；
          人工标为错误 <b>{{ annotationInfo.wrong }}</b> 条；
          其中 <b>{{ annotationInfo.excluded }}</b> 条因未给出正确答案被排除在评分之外。
        </div>
        <div class="grid grid-cols-2 gap-5">
          <div class="bg-canvas rounded-[10px] p-4">
            <div class="font-semibold mb-2">当前线上版本（对照基线）</div>
            <template v-for="(v, k) in metrics?.baseline || {}" :key="k">
              <div v-if="typeof v !== 'object' || v === null"
                class="flex justify-between text-[13px] py-1.5 border-b border-dashed border-line last:border-0">
                <span>{{ k }}</span><span class="font-semibold">{{ v }}</span>
              </div>
            </template>
            <div v-if="metrics?.baseline?.text" class="mt-2 pt-2 border-t border-line">
              <div class="text-xs text-t3 mb-1">文本指标（BLEU/ROUGE/F1，{{ metrics.baseline.text.cases }} 条标准答案）</div>
              <div v-for="(v, k) in metrics.baseline.text" :key="k" v-show="k !== 'cases'"
                class="flex justify-between text-[12px] py-0.5"><span>{{ k }}</span><span class="font-semibold">{{ v }}</span></div>
            </div>
          </div>
          <div class="bg-brand-lighter border border-brand rounded-[10px] p-4">
            <div class="font-semibold mb-2 text-brand">
              候选 Draft
              <span :class="candidate.recommendation === 'PROPOSE' ? 'badge badge-ok ml-1' : 'badge badge-gray ml-1'">
                {{ candidate.recommendation === 'PROPOSE' ? '建议采纳' : '不建议' }}
              </span>
            </div>
            <template v-for="(v, k) in metrics?.candidate || {}" :key="k">
              <div v-if="typeof v !== 'object' || v === null"
                class="flex justify-between text-[13px] py-1.5 border-b border-dashed border-line last:border-0">
                <span>{{ k }}</span><span class="font-semibold">{{ v }}</span>
              </div>
            </template>
            <div v-if="metrics?.candidate?.text" class="mt-2 pt-2 border-t border-line">
              <div class="text-xs text-t3 mb-1">文本指标（{{ metrics.candidate.text.cases }} 条标准答案）</div>
              <div v-for="(v, k) in metrics.candidate.text" :key="k" v-show="k !== 'cases'"
                class="flex justify-between text-[12px] py-0.5"><span>{{ k }}</span><span class="font-semibold">{{ v }}</span></div>
            </div>
          </div>
        </div>
        <div v-if="candidate.rationale" class="mt-4 text-t3 text-[13px] mono">{{ candidate.rationale }}</div>
        <div class="mt-4 text-t2 text-[13px]">优化只会生成候选稿，绝不自动上线。请在下方对比新旧差异后，决定发布还是驳回。</div>
      </div>

      <div v-if="candidate" class="mt-6 border-t border-line pt-5">
        <h3 class="font-semibold mb-1">候选稿审核</h3>
        <p class="text-t2 text-[13px] mb-4">左边是当前线上的提示词，右边是优化出的候选稿。绿色是新增内容，红色是删除内容。点「发布」后立即生效，下一次调用就会用新版。</p>
        <div v-if="failureCategories" class="mb-4 flex flex-wrap gap-2 text-[13px]">
          <span v-for="(n, cat) in failureCategories" :key="cat"
            class="badge badge-gray">失败归因 {{ cat }}: {{ n }}</span>
        </div>
        <div class="grid grid-cols-2 gap-4">
          <div class="border border-line rounded-[10px] overflow-hidden">
            <div class="px-4 py-2 bg-canvas font-semibold text-[13px]">当前线上版本</div>
            <div class="max-h-96 overflow-auto mono text-[12px] leading-6">
              <div v-for="(row, idx) in diffRows" :key="'l' + idx"
                :class="['px-4 min-h-6 whitespace-pre-wrap break-all',
                  row.kind === 'del' ? 'bg-[#fdecec] text-[#b4291f]' : 'text-t2']">{{ row.l }}</div>
            </div>
          </div>
          <div class="border border-brand rounded-[10px] overflow-hidden">
            <div class="px-4 py-2 bg-canvas font-semibold text-[13px] text-brand">优化候选稿</div>
            <div class="max-h-96 overflow-auto mono text-[12px] leading-6">
              <div v-for="(row, idx) in diffRows" :key="'r' + idx"
                :class="['px-4 min-h-6 whitespace-pre-wrap break-all',
                  row.kind === 'add' ? 'bg-[#e8f7ee] text-[#1c7c3c]' : 'text-t2']">{{ row.r }}</div>
            </div>
          </div>
        </div>
        <div class="flex items-center gap-3 mt-4">
          <button v-if="canManage" class="btn btn-primary"
            :disabled="reviewBusy || candidate.recommendation !== 'PROPOSE'"
            @click="approveCandidate">
            {{ reviewBusy ? '处理中…' : '通过并发布' }}
          </button>
          <button v-if="canManage" class="btn" :disabled="reviewBusy" @click="rejectCandidate">驳回</button>
          <span v-if="candidate.recommendation !== 'PROPOSE'" class="badge badge-gray">已驳回</span>
          <span v-if="reviewMsg" class="text-t2 text-[13px]">{{ reviewMsg }}</span>
        </div>
      </div>
    </div>
  </div>
</template>
