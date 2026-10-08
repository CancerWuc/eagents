<script setup>
// 性能测试 Tab（perf-test 01 §9，T16-T19）：
// 数据集管理 / 压力测试 / 仿真测试 三个子页；共享 Agent Team 选择。
// 启动后 3s 轮询活跃测试进度；全局锁冲突（409）经全局通知提示。
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api } from '../api.js'
import { promptText, confirmAction } from '../ui.js'
import { notify, success } from '../notify.js'
import { fmtDateTime } from '../format.js'

import Pagination from '../components/Pagination.vue'
import { useServerPagination } from '../pagination.js'

defineProps({ canManage: { type: Boolean, default: false } })

const tab = ref('datasets')
const masters = ref([])
const services = ref([])
const masterId = ref('')
const datasets = ref([])
const tests = ref([])
const loading = ref(false)

// ---- 数据集管理 ----
const showDsForm = ref(false)
const dsForm = ref({ name: '', dataset_type: 'PRESSURE' })
const preview = ref(null)      // { dataset, cases, page }
const uploadBusyId = ref('')

// ---- 测试配置（压测/仿真共用目标选择）----
const pForm = ref({
  datasetId: '', targetType: 'MASTER', targetId: '',
  concurrency: 10, totalRequests: 100, durationSec: 0, rampUpSec: 0,
  timeoutMs: 60000, capUser: api.user, caseOrder: '顺序轮询',
})
const sForm = ref({
  datasetId: '', targetType: 'MASTER', targetId: '',
  timeoutMs: 60000, capUser: api.user, maxConcurrency: 0,
})
const activeTest = ref(null)   // 进行中的测试详情（progress/report）
const analysis = ref(null)      // DEV-09 LLM 分析报告 {analysis_md, analysis_model}
const analysisBusy = ref(false)
async function loadAnalysis(tid) {
  analysis.value = null
  try { analysis.value = await api.getPerfAnalysis(tid) } catch { /* 尚未生成 → 404 静默 */ }
}
async function genAnalysis() {
  if (!activeTest.value) return
  analysisBusy.value = true
  try {
    const out = await api.analyzePerfTest(activeTest.value.perf_test_id)
    analysis.value = await api.getPerfAnalysis(activeTest.value.perf_test_id)
    success(`分析报告已生成（数字防编造校验通过，来源 ${out.analysis_model}）`)
  } finally { analysisBusy.value = false }
}
const activeSamples = ref(null) // { testId, rows, page, status }
const detailTest = ref(null)   // 历史详情展开

let pollTimer = null

const datasetOptions = ref([])
const previewDatasetId = ref('')
const previewCases = ref([])
const samplesTestId = ref('')
const samplesStatus = ref('')
const sampleRows = ref([])
const datasetsPager = useServerPagination(datasets, () => `/api/v1/perf/datasets?masterId=${masterId.value}`)
const testsPager = useServerPagination(tests, () => `/api/v1/perf/tests?masterId=${masterId.value}&testType=${tab.value === 'simulation' ? 'SIMULATION' : 'PRESSURE'}`, {
  onResult: () => { detailTest.value = null },
})
const previewPager = useServerPagination(previewCases, () => `/api/v1/perf/datasets/${previewDatasetId.value}`, {
  onResult: result => { preview.value = { ...result, cases: result.items } },
})
const samplesPager = useServerPagination(sampleRows, () => `/api/v1/perf/tests/${samplesTestId.value}/samples?status=${samplesStatus.value}`, {
  onResult: result => { activeSamples.value = { testId: samplesTestId.value, rows: result.items, page: result.page, status: samplesStatus.value } },
})
watch(tab, () => { if (masterId.value) testsPager.load().catch(() => {}) })
const master = computed(() => masters.value.find(m => m.master_id === masterId.value) || null)
const pressureDatasets = computed(() => datasetOptions.value.filter(d => d.dataset_type === 'PRESSURE'))
const simulationDatasets = computed(() => datasetOptions.value.filter(d => d.dataset_type === 'SIMULATION'))
const activeRunning = computed(() => activeTest.value && ['PENDING', 'RUNNING'].includes(activeTest.value.status))
const simPlan = computed(() => {
  // 仿真数据集用例的计划触发时间（今天未到今天发，已过明天发，01 §7.5）
  if (!preview.value || preview.value.dataset.dataset_type !== 'SIMULATION') return []
  const now = new Date()
  const todaySec = now.getHours() * 3600 + now.getMinutes() * 60 + now.getSeconds()
  return preview.value.cases.map(c => {
    const [h, m, s] = (c.send_time || '00:00:00').split(':').map(Number)
    const sec = h * 3600 + m * 60 + s
    const day = sec > todaySec ? '今天' : '明天'
    const d = new Date(now)
    if (sec <= todaySec) d.setDate(d.getDate() + 1)
    d.setHours(h, m, s, 0)
    return { seq_no: c.seq_no, q: c.q, send_time: c.send_time, day, at: d }
  })
})

function statusBadge(s) {
  return { COMPLETED: 'badge badge-ok', RUNNING: 'badge badge-warn', PENDING: 'badge badge-gray',
    FAILED: 'badge badge-err', CANCELED: 'badge badge-gray', SUCCEEDED: 'badge badge-ok',
    TIMEOUT: 'badge badge-err', REJECTED: 'badge badge-warn' }[s] || 'badge badge-gray'
}
function fmtTime(t) { return (t || '').replace('T', ' ').slice(0, 19) }
function fmtSec(t) {
  if (!t) return '—'
  const d = new Date(t)
  return d.toLocaleTimeString('zh-CN', { hour12: false })
}

async function loadAgents() {
  const [agentOut, serviceOut] = await Promise.all([
    api.agentSelector('performance'), api.serviceSelector('performance'),
  ])
  masters.value = agentOut.items.map(m => ({ master_id: m.masterId, slug: m.slug, display_name: m.displayName }))
  services.value = serviceOut.items.map(s => ({ service_id: s.serviceId, slug: s.slug, display_name: s.displayName }))
  if (!masterId.value && masters.value.length) masterId.value = masters.value[0].master_id
}
async function loadAll() {
  if (!masterId.value) return
  const id = masterId.value
  loading.value = true
  try {
    await Promise.all([datasetsPager.load(), testsPager.load()])
    const ds = await api.listPerfDatasets(id)
    if (masterId.value === id) datasetOptions.value = ds.datasets
  } finally { loading.value = false }
}
function closeSamples() { samplesPager.invalidate(); activeSamples.value = null }
function closePreview() { previewPager.invalidate(); preview.value = null }
function closeTest() { stopPolling(); activeTest.value = null; closeSamples() }
async function onMasterChange() {
  datasetsPager.invalidate(); testsPager.invalidate()
  closePreview(); closeTest(); detailTest.value = null
  await loadAll()
}

// ---- 数据集管理（T17）----
async function createDataset() {
  if (!dsForm.value.name) return
  await api.createPerfDataset({ masterId: masterId.value, name: dsForm.value.name, datasetType: dsForm.value.dataset_type })
  showDsForm.value = false
  dsForm.value = { name: '', dataset_type: 'PRESSURE' }
  await loadAll()
}
async function onUpload(e, ds) {
  const file = e.target.files?.[0]
  e.target.value = ''
  if (!file) return
  if (!/\.(csv|xlsx)$/i.test(file.name)) {
    notify({ code: 'VALIDATION_ERROR', message: '仅支持 CSV 与 XLSX 文件' })
    return
  }
  uploadBusyId.value = ds.perf_dataset_id
  try {
    const out = await api.uploadPerfDataset(ds.perf_dataset_id, file)
    notify({ code: 'UPLOADED', message: `已解析 ${out.case_count} 条用例${out.replaced ? '（覆盖旧数据）' : ''}` })
    await loadAll()
    if (preview.value?.dataset.perf_dataset_id === ds.perf_dataset_id) await openPreview(ds)
  } finally { uploadBusyId.value = '' }
}
async function openPreview(ds, page = 1) {
  if (!ds) return
  previewDatasetId.value = ds.perf_dataset_id
  await previewPager.load({ page })
}
async function removeDataset(ds) {
  if (!await confirmAction(`删除数据集「${ds.name}」及其全部用例？此操作不可恢复。`, { okText: '删除', danger: true })) return
  await api.deletePerfDataset(ds.perf_dataset_id)
  if (preview.value?.dataset.perf_dataset_id === ds.perf_dataset_id) preview.value = null
  await loadAll()
}

// ---- 目标选择 ----
const targetOptions = computed(() => [
  { type: 'MASTER', id: masterId.value, label: `Team：${master.value?.display_name || ''}（标识：${master.value?.slug || ''}）` },
  ...services.value.map(s => ({ type: 'SERVICE', id: s.service_id, label: `服务：${s.display_name}（标识：${s.slug}）${s.enabled ? '' : ' [已禁用]'}` })),
])

// ---- 压力测试（T18）----
function syncTarget(form) {
  const opt = targetOptions.value.find(o => o.id === form.targetId && o.type === form.targetType)
  if (!opt) {
    const first = targetOptions.value[0]
    form.targetType = first?.type || 'MASTER'
    form.targetId = first?.id || ''
  }
}
async function startPressure() {
  const f = pForm.value
  syncTarget(f)
  const body = {
    masterId: masterId.value, datasetId: f.datasetId,
    targetType: f.targetType, targetId: f.targetId, testType: 'PRESSURE',
    pressure: {
      concurrency: Number(f.concurrency) || 1,
      totalRequests: Number(f.totalRequests) || 0,
      durationSec: Number(f.durationSec) || 0,
      rampUpSec: Number(f.rampUpSec) || 0,
      timeoutMs: Number(f.timeoutMs) || 60000,
      capUser: f.capUser || api.user,
      caseOrder: f.caseOrder,
    },
  }
  const test = await api.createPerfTest(body)
  await watchTest(test.perf_test_id)
}

// ---- 仿真测试（T19）----
async function startSimulation() {
  const f = sForm.value
  syncTarget(f)
  const body = {
    masterId: masterId.value, datasetId: f.datasetId,
    targetType: f.targetType, targetId: f.targetId, testType: 'SIMULATION',
    simulation: {
      timeoutMs: Number(f.timeoutMs) || 60000,
      capUser: f.capUser || api.user,
      maxConcurrency: Number(f.maxConcurrency) || 0,
    },
  }
  const test = await api.createPerfTest(body)
  await watchTest(test.perf_test_id)
}

// ---- 进度轮询（2-5s）----
async function watchTest(testId) {
  activeTest.value = await api.getPerfTest(testId)

  await loadAnalysis(testId)
  closeSamples()
  await loadAll()  // 历史列表立即可见新测试（否则仅在完成时刷新）
  startPolling()
}
function startPolling() {
  stopPolling()
  pollTimer = setInterval(async () => {
    if (!activeTest.value) return stopPolling()
    const id = activeTest.value.perf_test_id
    try {
      const result = await api.getPerfTest(id)
      if (activeTest.value?.perf_test_id !== id) return
      activeTest.value = result
      if (activeSamples.value && activeTest.value.perf_test_id === activeSamples.value.testId) {
        await loadSamples(activeTest.value.perf_test_id, activeSamples.value.page, activeSamples.value.status)
      }
      if (!['PENDING', 'RUNNING'].includes(activeTest.value.status)) await loadAll()
    } catch { /* 轮询失败静默，下一轮重试 */ }
  }, 3000)
}
function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null } }
onBeforeUnmount(stopPolling)

async function cancelActive() {
  if (!activeTest.value) return
  await api.cancelPerfTest(activeTest.value.perf_test_id)
  activeTest.value = await api.getPerfTest(activeTest.value.perf_test_id)
}
function qps(t) {
  if (!t?.started_at || !t.progress?.sent) return '—'
  const elapsed = ((t.status === 'RUNNING' ? Date.now() : new Date(t.finished_at || Date.now())) - new Date(t.started_at)) / 1000
  return elapsed > 0 ? (t.progress.sent / elapsed).toFixed(1) : '—'
}

// ---- 历史 / 样本 ----
async function openHistory(t) {
  detailTest.value = t.perf_test_id === detailTest.value?.perf_test_id ? null : await api.getPerfTest(t.perf_test_id)
}
async function loadSamples(testId, page = samplesPager.page, status = '') {
  samplesTestId.value = testId
  samplesStatus.value = status
  await samplesPager.load({ page })
}
function openSamples(t) {
  activeTest.value = t
  loadSamples(t.perf_test_id)
  startPolling()
}
function isMac(t) { return t?.target_type === 'MASTER' }
function targetLabel(t) {
  if (isMac(t)) return `Team ${t.config?.slug || t.target_id}`
  return `服务 ${t.config?.slug || t.config?.targetName || t.target_id}`
}

onMounted(async () => {
  await loadAgents()
  await loadAll()
  // 恢复展示未完成的测试（如刷新页面）
  const active = await api.get(`/api/v1/perf/tests?masterId=${masterId.value}&active=true&page=1&pageSize=10`)
  const running = active.items[0]
  if (running) await watchTest(running.perf_test_id)
})
</script>

<template>
  <div>
    <div class="flex items-center justify-between gap-6 mb-5">
      <div class="min-w-0">
        <h2 class="text-xl font-semibold">性能测试</h2>
        <p class="text-sm text-t3 mt-1">对 Agent Team 或已注册的服务做压力测试。上传一批问题（CSV/XLSX）→ 由独立进程按设定的并发发起调用 → 输出延迟、成功率等指标。</p>
      </div>
      <select v-model="masterId" class="input max-w-xs !w-auto shrink-0" @change="onMasterChange">
        <option v-for="m in masters" :key="m.master_id" :value="m.master_id">{{ m.display_name }}（标识：{{ m.slug }}）</option>
      </select>
    </div>

    <div class="flex gap-1 border-b border-line mb-5">
      <button v-for="t in [['datasets','测试数据集管理'],['pressure','压力测试'],['simulation','仿真测试']]" :key="t[0]"
              :class="['px-4 h-10 text-sm font-medium border-b-2 -mb-px cursor-pointer', tab === t[0] ? 'border-brand text-brand' : 'border-transparent text-t2 hover:text-t1']"
              @click="tab = t[0]">{{ t[1] }}</button>
    </div>

    <!-- ============ 数据集管理（T17）============ -->
    <div v-if="tab === 'datasets'" class="space-y-4">
      <div class="card">
        <div class="flex items-center justify-between mb-3">
          <div class="text-sm text-t2">属于当前 Team 的数据集。整个平台同一时间只允许跑一个测试。</div>
          <button v-if="canManage" class="btn btn-primary" @click="showDsForm = true">+ 新建数据集</button>
        </div>
        <table class="tbl">
          <thead><tr><th>名称</th><th>类型</th><th>用例数</th><th>创建时间</th><th>操作</th></tr></thead>
          <tbody>
            <tr v-if="!datasets.length && !loading"><td colspan="5" class="text-center text-t3 py-8">还没有数据集。新建一个，然后上传 CSV/XLSX</td></tr>
            <tr v-for="ds in datasets" :key="ds.perf_dataset_id">
              <td class="font-medium">{{ ds.name }}</td>
              <td><span :class="ds.dataset_type === 'PRESSURE' ? 'badge badge-brand' : 'badge badge-warn'">{{ ds.dataset_type === 'PRESSURE' ? '压力' : '仿真' }}</span></td>
              <td>{{ ds.case_count }}</td>
              <td class="text-xs text-t3">{{ fmtTime(ds.created_at) }}</td>
              <td class="whitespace-nowrap">
                <input type="file" class="hidden" :id="'perf-up-' + ds.perf_dataset_id" :disabled="uploadBusyId === ds.perf_dataset_id" @change="e => onUpload(e, ds)" />
                <button v-if="canManage" class="btn btn-text" :disabled="uploadBusyId === ds.perf_dataset_id" @click="document.getElementById('perf-up-' + ds.perf_dataset_id).click()">
                  {{ uploadBusyId === ds.perf_dataset_id ? '上传中…' : '上传 CSV/XLSX' }}
                </button>
                <button class="btn btn-text" @click="openPreview(ds)">用例预览</button>
                <button v-if="canManage" class="btn btn-text text-red-500" @click="removeDataset(ds)">删除</button>
              </td>
            </tr>
          </tbody>
        </table>
        <Pagination :pager="datasetsPager" />
      </div>

      <!-- 用例预览 / 仿真计划时间 -->
      <div v-if="preview" class="card !p-0">
        <div class="flex items-center justify-between px-4 pt-4">
          <div class="font-semibold text-sm">{{ preview.dataset.name }} · {{ preview.dataset.case_count }} 条用例</div>
          <div class="flex items-center gap-2">
            <button class="btn btn-text" @click="closePreview">关闭</button>
          </div>
        </div>
        <table class="tbl">
          <thead><tr>
            <th>#</th><th>q</th><th>期望答案（可选，用于算答案匹配率）</th>
            <th v-if="preview.dataset.dataset_type === 'SIMULATION'">time（每日时刻）</th>
            <th v-if="preview.dataset.dataset_type === 'SIMULATION'">下次触发（今天/明天）</th>
          </tr></thead>
          <tbody>
            <tr v-for="c in preview.cases" :key="c.case_id || c.seq_no">
              <td>{{ c.seq_no }}</td>
              <td class="max-w-[420px] truncate" :title="c.q">{{ c.q }}</td>
              <td class="max-w-[280px] truncate text-t2" :title="c.a">{{ c.a || '—' }}</td>
              <template v-if="preview.dataset.dataset_type === 'SIMULATION'">
                <td class="font-mono text-xs">{{ c.send_time }}</td>
                <td class="text-xs">
                  <span v-if="simPlan.find(p => p.seq_no === c.seq_no)" :class="simPlan.find(p => p.seq_no === c.seq_no).day === '今天' ? 'text-up' : 'text-t3'">
                    {{ simPlan.find(p => p.seq_no === c.seq_no).day }} {{ c.send_time }}
                  </span>
                </td>
              </template>
            </tr>
          </tbody>
        </table>
        <Pagination :pager="previewPager" />
      </div>

      <!-- 新建数据集 -->
      <div v-if="showDsForm" class="fixed inset-0 bg-black/30 flex items-center justify-center z-50" @click.self="showDsForm = false">
        <div class="bg-white rounded-xl p-6 w-[440px] shadow-xl">
          <h3 class="font-semibold mb-4">新建数据集</h3>
          <div class="space-y-3">
            <div><label class="text-xs text-t2">名称</label><input v-model="dsForm.name" class="input" placeholder="客服高频问题-1000条" /></div>
            <div>
              <label class="text-xs text-t2">类型</label>
              <select v-model="dsForm.dataset_type" class="input">
                <option value="PRESSURE">压力测试 —— 尽可能快地发请求，测极限（列：问题[,期望答案]）</option>
                <option value="SIMULATION">仿真测试 —— 按真实时刻还原一天的请求分布（列：问题,时刻[,期望答案]）</option>
              </select>
            </div>
            <div class="text-xs text-t3">上传 CSV（UTF-8 编码）或 XLSX，第一行是表头。仿真测试必须有 time 列，格式 HH:mm:ss</div>
          </div>
          <div class="flex justify-end gap-2 mt-4">
            <button class="btn btn-ghost" @click="showDsForm = false">取消</button>
            <button class="btn btn-primary" @click="createDataset">创建</button>
          </div>
        </div>
      </div>
    </div>

    <!-- ============ 压力测试（T18）============ -->
    <div v-if="tab === 'pressure'" class="space-y-4">
      <div class="card">
        <div class="grid grid-cols-3 gap-3 mb-3">
          <div>
            <label class="text-xs text-t2">数据集（PRESSURE）</label>
            <select v-model="pForm.datasetId" class="input">
              <option value="">选择数据集…</option>
              <option v-for="d in pressureDatasets" :key="d.perf_dataset_id" :value="d.perf_dataset_id">{{ d.name }}（{{ d.case_count }} 条）</option>
            </select>
          </div>
          <div>
            <label class="text-xs text-t2">测试目标</label>
            <select v-model="pForm.targetId" class="input" @change="pForm.targetType = (targetOptions.find(o => o.id === pForm.targetId) || {}).type || 'MASTER'">
              <option v-for="o in targetOptions" :key="o.type + o.id" :value="o.id">{{ o.label }}</option>
            </select>
          </div>
          <div>
            <label class="text-xs text-t2">以哪个工号的身份调用（影响可见的服务范围）</label>
            <input v-model="pForm.capUser" class="input" />
          </div>
        </div>
        <div class="grid grid-cols-5 gap-3 mb-3">
          <div><label class="text-xs text-t2">并发</label><input v-model.number="pForm.concurrency" type="number" min="1" class="input" /></div>
          <div><label class="text-xs text-t2">总请求数</label><input v-model.number="pForm.totalRequests" type="number" min="0" class="input" /></div>
          <div><label class="text-xs text-t2">持续秒数</label><input v-model.number="pForm.durationSec" type="number" min="0" class="input" /></div>
          <div><label class="text-xs text-t2">压力爬坡时间（秒，从 0 逐步加到目标并发）</label><input v-model.number="pForm.rampUpSec" type="number" min="0" class="input" /></div>
          <div><label class="text-xs text-t2">单次请求超时（毫秒）</label><input v-model.number="pForm.timeoutMs" type="number" min="1000" class="input" /></div>
        </div>
        <div class="flex items-center gap-3">
          <div class="flex items-center gap-2 text-sm text-t2">
            <span>用例取用顺序</span>
            <select v-model="pForm.caseOrder" class="input !py-1"><option>顺序轮询</option><option>随机抽取</option></select>
          </div>
          <button v-if="canManage" class="btn btn-primary" :disabled="!pForm.datasetId || activeRunning" @click="startPressure">启动压测</button>
          <span v-if="activeRunning" class="text-xs text-t3">已有测试在跑，需要先等它结束或手动取消</span>
          <span v-else-if="!pForm.datasetId" class="text-xs text-t3">先选一个数据集</span>
          <span v-else class="text-xs text-t3">总请求数和持续时间至少填一个，哪个先到就停</span>
        </div>
      </div>

      <!-- 实时进度 -->
      <div v-if="activeTest" class="card">
        <div class="flex items-center gap-2 mb-3">
          <span class="font-mono text-xs text-t3">{{ activeTest.perf_test_id }}</span>
          <span :class="statusBadge(activeTest.status)">{{ activeTest.status }}</span>
          <span class="text-xs text-t2">{{ activeTest.test_type }} · {{ targetLabel(activeTest) }}</span>
          <span v-if="activeTest.cancelRequested && activeTest.status === 'RUNNING'" class="badge badge-warn">取消中…</span>
          <div class="flex-1"></div>
          <button v-if="canManage && ['PENDING', 'RUNNING'].includes(activeTest.status)" class="btn btn-ghost btn-sm text-red-500" @click="cancelActive">取消</button>
          <button class="btn btn-ghost btn-sm" @click="closeTest">关闭</button>
        </div>
        <div class="grid grid-cols-7 gap-4 mb-3">
          <div><div class="text-xs text-t3">已发送 / 计划总数</div><div class="text-lg font-semibold">{{ activeTest.progress.sent }}/{{ activeTest.progress.plannedTotal ?? '—' }}</div></div>
          <div><div class="text-xs text-t3">成功</div><div class="text-lg font-semibold text-up">{{ activeTest.progress.succeeded }}</div></div>
          <div><div class="text-xs text-t3">失败</div><div class="text-lg font-semibold text-brand">{{ activeTest.progress.failed }}</div></div>
          <div><div class="text-xs text-t3">超时</div><div class="text-lg font-semibold text-brand">{{ activeTest.progress.timedOut }}</div></div>
          <div><div class="text-xs text-t3">拒绝</div><div class="text-lg font-semibold text-warn">{{ activeTest.progress.rejected }}</div></div>
          <div><div class="text-xs text-t3">QPS</div><div class="text-lg font-semibold">{{ qps(activeTest) }}</div></div>
          <div><div class="text-xs text-t3">平均延迟</div><div class="text-lg font-semibold">{{ activeTest.progress.avgLatencyMs != null ? activeTest.progress.avgLatencyMs + 'ms' : '—' }}</div></div>
        </div>
        <div v-if="activeTest.nextFireAt" class="text-sm text-warn mb-2">下次触发：{{ fmtTime(activeTest.nextFireAt) }}（到点自动发送，期间可取消）</div>

        <!-- 报告（完成后） -->
        <div v-if="activeTest.report" class="border border-line rounded-lg p-3 text-sm">
          <div class="font-medium mb-2">报告</div>
          <div class="grid grid-cols-4 gap-3 text-xs">
            <div>成功率 <b>{{ (activeTest.report.summary.successRate * 100).toFixed(1) }}%</b></div>
            <div>吞吐 <b>{{ activeTest.report.summary.throughput ?? '—' }}</b> req/s</div>
            <div>答案匹配率 <b>{{ activeTest.report.summary.answerMatchRate != null ? (activeTest.report.summary.answerMatchRate * 100).toFixed(1) + '%' : '—' }}</b></div>
            <div>requires_input <b>{{ activeTest.report.summary.requiresInput }}</b></div>
          </div>
          <div class="grid grid-cols-6 gap-3 text-xs mt-2">
            <div v-for="(v, k) in activeTest.report.summary.latencyMs" :key="k">{{ k }} <b>{{ v != null ? v + 'ms' : '—' }}</b></div>
          </div>
          <div v-if="Object.keys(activeTest.report.summary.errorBreakdown || {}).length" class="text-xs mt-2 text-t2">
            错误分布：<span v-for="(n, c) in activeTest.report.summary.errorBreakdown" :key="c" class="badge badge-err mr-1">{{ c }} × {{ n }}</span>
          </div>
        </div>
        <!-- DEV-09: LLM 分析报告（数字防编造校验，一键生成/覆盖） -->
        <div v-if="activeTest.report" class="border border-line rounded-lg p-3 text-sm mt-3">
          <div class="flex items-center justify-between mb-2">
            <div class="font-medium">LLM 分析报告</div>
            <button v-if="canManage" class="btn btn-primary btn-sm" :disabled="analysisBusy" @click="genAnalysis">
              {{ analysisBusy ? '生成中…' : analysis ? '重新生成' : '生成分析' }}
            </button>
          </div>
          <div v-if="analysis" class="text-xs text-t3 mb-1">模型 {{ analysis.analysis_model }} ｜ {{ fmtDateTime(analysis.analysis_at) }}（报告中每个数字均来自实测数据）</div>
          <div v-else class="text-xs text-t3">基于结构化报告 + 基线对比生成瓶颈定位与建议；未配置 LLM 时无法生成。</div>
          <pre v-if="analysis" class="bg-canvas rounded-lg p-3 text-xs whitespace-pre-wrap mt-1 max-h-96 overflow-auto">{{ analysis.analysis_md }}</pre>
        </div>
        <button class="btn btn-ghost btn-sm mt-2" @click="activeSamples ? closeSamples() : openSamples(activeTest)">
          {{ activeSamples ? '收起样本' : '查看样本明细' }}
        </button>
        <table v-if="activeSamples" class="tbl mt-2">
          <thead><tr><th>#</th><th>q</th><th>状态</th><th>延迟</th><th>匹配</th><th>trace</th><th>sent_at</th></tr></thead>
          <tbody>
            <tr v-if="!activeSamples.rows.length"><td colspan="7" class="text-center text-t3 py-6">暂无样本</td></tr>
            <tr v-for="s in activeSamples.rows" :key="s.sample_id">
              <td>{{ s.seq_no }}</td>
              <td class="max-w-[260px] truncate" :title="s.q">{{ s.q }}</td>
              <td><span :class="statusBadge(s.status)">{{ s.status }}</span></td>
              <td>{{ s.latency_ms != null ? s.latency_ms + 'ms' : '—' }}</td>
              <td>{{ s.answer_match == null ? '—' : (s.answer_match ? '✓' : '✗') }}</td>
              <td class="font-mono text-xs text-t3">{{ s.trace_id || '—' }}</td>
              <td class="text-xs text-t3">{{ fmtTime(s.sent_at) }}</td>
            </tr>
          </tbody>
        </table>
        <Pagination v-if="activeSamples" :pager="samplesPager" />
      </div>

      <!-- 历史 -->
      <div class="card !p-0">
        <div class="px-4 pt-4 font-semibold text-sm">历史测试</div>
        <table class="tbl">
          <thead><tr><th>测试</th><th>类型</th><th>对象</th><th>状态</th><th>创建</th><th>开始/结束</th><th></th></tr></thead>
          <tbody>
            <tr v-if="!tests.length"><td colspan="7" class="text-center text-t3 py-6">还没有跑过测试</td></tr>
            <template v-for="t in tests" :key="t.perf_test_id">
              <tr>
                <td class="font-mono text-xs">{{ t.perf_test_id }}</td>
                <td>{{ t.test_type }}</td>
                <td class="text-xs">{{ t.target_type }} {{ t.target_id.slice(0, 12) }}…</td>
                <td><span :class="statusBadge(t.status)">{{ t.status }}</span></td>
                <td class="text-xs text-t3">{{ fmtTime(t.created_at) }}</td>
                <td class="text-xs text-t3">{{ fmtTime(t.started_at) }}<br />{{ fmtTime(t.finished_at) }}</td>
                <td><button class="btn btn-text" @click="openHistory(t)">{{ detailTest?.perf_test_id === t.perf_test_id ? '收起' : '详情' }}</button></td>
              </tr>
              <tr v-if="detailTest?.perf_test_id === t.perf_test_id">
                <td colspan="7" class="bg-canvas/60">
                  <div class="p-3 text-xs space-y-1">
                    <div>数据集 {{ detailTest.perf_dataset_id }} · 目标 {{ targetLabel(detailTest) }} · 端点 <span class="font-mono">{{ detailTest.config?.endpoint }}</span></div>
                    <div v-if="detailTest.report">成功率 {{ (detailTest.report.summary.successRate * 100).toFixed(1) }}% · p95 {{ detailTest.report.summary.latencyMs?.p95 ?? '—' }}ms · p99 {{ detailTest.report.summary.latencyMs?.p99 ?? '—' }}ms · 吞吐 {{ detailTest.report.summary.throughput ?? '—' }} req/s · 匹配率 {{ detailTest.report.summary.answerMatchRate != null ? (detailTest.report.summary.answerMatchRate * 100).toFixed(1) + '%' : '—' }}</div>
                    <div v-else>暂无报告</div>
                    <button class="btn btn-text" @click="tab === 'pressure' ? openSamples(detailTest) : null; activeTest = detailTest">在上方查看进度与样本</button>
                  </div>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
        <Pagination :pager="testsPager" />
      </div>
    </div>

    <!-- ============ 仿真测试（T19）============ -->
    <div v-if="tab === 'simulation'" class="space-y-4">
      <div class="card">
        <div class="grid grid-cols-3 gap-3 mb-3">
          <div>
            <label class="text-xs text-t2">数据集（SIMULATION）</label>
            <select v-model="sForm.datasetId" class="input" @change="sForm.datasetId && openPreview(datasetOptions.find(d => d.perf_dataset_id === sForm.datasetId))">
              <option value="">选择数据集…</option>
              <option v-for="d in simulationDatasets" :key="d.perf_dataset_id" :value="d.perf_dataset_id">{{ d.name }}（{{ d.case_count }} 条）</option>
            </select>
          </div>
          <div>
            <label class="text-xs text-t2">测试目标</label>
            <select v-model="sForm.targetId" class="input" @change="sForm.targetType = (targetOptions.find(o => o.id === sForm.targetId) || {}).type || 'MASTER'">
              <option v-for="o in targetOptions" :key="o.type + o.id" :value="o.id">{{ o.label }}</option>
            </select>
          </div>
          <div>
            <label class="text-xs text-t2">以哪个工号的身份调用（影响可见的服务范围）</label>
            <input v-model="sForm.capUser" class="input" />
          </div>
        </div>
        <div class="grid grid-cols-3 gap-3 mb-3">
          <div><label class="text-xs text-t2">单次请求超时（毫秒）</label><input v-model.number="sForm.timeoutMs" type="number" min="1000" class="input" /></div>
          <div><label class="text-xs text-t2">最大并发数（填 0 表示不限制）</label><input v-model.number="sForm.maxConcurrency" type="number" min="0" class="input" /></div>
        </div>
        <div class="flex items-center gap-3">
          <button v-if="canManage" class="btn btn-primary" :disabled="!sForm.datasetId || activeRunning" @click="startSimulation">启动仿真</button>
          <!-- 与压测一侧保持同一套禁用提示，否则同样是灰按钮却只有一边说明原因 -->
          <span v-if="activeRunning" class="text-xs text-t3">已有测试在跑，需要先等它结束或手动取消</span>
          <span v-else-if="!sForm.datasetId" class="text-xs text-t3">先选一个数据集</span>
          <span v-else class="text-xs text-t3">按数据集里每条记录的时刻发送：今天还没到的今天发，已经过了的排到明天同一时刻</span>
        </div>
      </div>

      <!-- 计划触发时间（T19 验收点）-->
      <div v-if="preview && preview.dataset.dataset_type === 'SIMULATION'" class="card !p-0">
        <div class="px-4 pt-4 font-semibold text-sm">计划触发时间 · {{ preview.dataset.name }}</div>
        <table class="tbl">
          <thead><tr><th>#</th><th>q</th><th>每日时刻</th><th>下次触发</th></tr></thead>
          <tbody>
            <tr v-for="p in simPlan" :key="p.seq_no">
              <td>{{ p.seq_no }}</td>
              <td class="max-w-[420px] truncate" :title="p.q">{{ p.q }}</td>
              <td class="font-mono text-xs">{{ p.send_time }}</td>
              <td><span :class="p.day === '今天' ? 'text-up font-medium' : 'text-t3'">{{ p.day }} {{ p.send_time }}</span></td>
            </tr>
          </tbody>
        </table>
        <Pagination :pager="previewPager" />
        <div class="px-4 py-2 text-xs text-t3">仅展示当前页（{{ preview.cases.length }} 条）的计划时间</div>
      </div>

      <!-- 复用进度/历史展示 -->
      <div v-if="activeTest" class="card">
        <div class="flex items-center gap-2 mb-3">
          <span class="font-mono text-xs text-t3">{{ activeTest.perf_test_id }}</span>
          <span :class="statusBadge(activeTest.status)">{{ activeTest.status }}</span>
          <span class="text-xs text-t2">{{ activeTest.test_type }} · {{ targetLabel(activeTest) }}</span>
          <span v-if="activeTest.cancelRequested && activeTest.status === 'RUNNING'" class="badge badge-warn">取消中…</span>
          <div class="flex-1"></div>
          <button v-if="canManage && ['PENDING', 'RUNNING'].includes(activeTest.status)" class="btn btn-ghost btn-sm text-red-500" @click="cancelActive">取消</button>
          <button class="btn btn-ghost btn-sm" @click="closeTest">关闭</button>
        </div>
        <div class="grid grid-cols-5 gap-4 mb-3">
          <div><div class="text-xs text-t3">已完成 / 计划总数</div><div class="text-lg font-semibold">{{ activeTest.progress.sent }}/{{ activeTest.progress.plannedTotal ?? '—' }}</div></div>
          <div><div class="text-xs text-t3">成功</div><div class="text-lg font-semibold text-up">{{ activeTest.progress.succeeded }}</div></div>
          <div><div class="text-xs text-t3">失败</div><div class="text-lg font-semibold text-brand">{{ activeTest.progress.failed + activeTest.progress.timedOut }}</div></div>
          <div><div class="text-xs text-t3">下次触发</div><div class="text-lg font-semibold">{{ activeTest.nextFireAt ? fmtSec(activeTest.nextFireAt) : '—' }}</div></div>
          <div><div class="text-xs text-t3">平均延迟</div><div class="text-lg font-semibold">{{ activeTest.progress.avgLatencyMs != null ? activeTest.progress.avgLatencyMs + 'ms' : '—' }}</div></div>
        </div>
        <div v-if="activeTest.report" class="text-xs text-t2 border border-line rounded-lg p-3">
          成功率 {{ (activeTest.report.summary.successRate * 100).toFixed(1) }}% · p95 {{ activeTest.report.summary.latencyMs?.p95 ?? '—' }}ms · 吞吐 {{ activeTest.report.summary.throughput ?? '—' }} req/s
          · 匹配率 {{ activeTest.report.summary.answerMatchRate != null ? (activeTest.report.summary.answerMatchRate * 100).toFixed(1) + '%' : '—' }}
        </div>
      </div>

      <div class="card !p-0">
        <div class="px-4 pt-4 font-semibold text-sm">历史测试</div>
        <table class="tbl">
          <thead><tr><th>测试</th><th>对象</th><th>状态</th><th>创建</th><th>开始/结束</th><th></th></tr></thead>
          <tbody>
            <tr v-if="!tests.length"><td colspan="6" class="text-center text-t3 py-6">还没有跑过测试</td></tr>
            <tr v-for="t in tests" :key="t.perf_test_id">
              <td class="font-mono text-xs">{{ t.perf_test_id }}</td>
              <td class="text-xs">{{ t.target_type }}</td>
              <td><span :class="statusBadge(t.status)">{{ t.status }}</span></td>
              <td class="text-xs text-t3">{{ fmtTime(t.created_at) }}</td>
              <td class="text-xs text-t3">{{ fmtTime(t.started_at) }}<br />{{ fmtTime(t.finished_at) }}</td>
              <td><button class="btn btn-text" @click="watchTest(t.perf_test_id)">查看</button></td>
            </tr>
          </tbody>
        </table>
        <Pagination :pager="testsPager" />
      </div>
    </div>
  </div>
</template>
