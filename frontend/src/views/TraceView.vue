<script setup>
import { computed, ref, watch } from 'vue'
import { api } from '../api.js'

const props = defineProps({
  initialTraceId: { type: String, default: '' },
  canGoBack: { type: Boolean, default: false },
  returnLabel: { type: String, default: '返回上一页' },
})
defineEmits(['back'])

const traceId = ref('')
const trace = ref(null)
const err = ref('')
const loading = ref(false)
const mastersById = ref({})
const servicesById = ref({})

async function query() {
  const id = traceId.value.trim()
  if (!id || loading.value) return
  loading.value = true
  try {
    const [traceData, agents, services] = await Promise.all([
      api.getTrace(id), api.agentSelector('trace'), api.serviceSelector('trace'),
    ])
    trace.value = traceData
    mastersById.value = Object.fromEntries(
      (agents.items || []).map(item => [item.masterId, {
        master_id: item.masterId, display_name: item.displayName, slug: item.slug,
      }]))
    servicesById.value = Object.fromEntries(
      (services.items || []).map(item => [item.serviceId, {
        service_id: item.serviceId, display_name: item.displayName, slug: item.slug,
      }]))
    err.value = ''
  } catch (e) {
    trace.value = null
    err.value = e.message
  } finally {
    loading.value = false
  }
}

watch(() => props.initialTraceId, id => {
  if (!id) return
  traceId.value = id
  query()
}, { immediate: true })

function childrenOf(list, parent) {
  return list.filter(item => (item.parentInvocationId || null) === parent)
}

/** 递归拉平成带 depth 的完整树；同时识别同一目标的重复调用。 */
const traceNodes = computed(() => {
  if (!trace.value) return []
  const rows = []
  const walk = (parent, depth) => {
    for (const inv of childrenOf(trace.value.invocations, parent)) {
      rows.push({ inv, depth })
      walk(inv.invocationId, depth + 1)
    }
  }
  walk(null, 0)

  const totals = {}
  const seen = {}
  for (const row of rows) {
    const inv = row.inv
    const key = `${inv.parentInvocationId || 'root'}:${inv.serviceId || inv.masterId || inv.invocationId}`
    totals[key] = (totals[key] || 0) + 1
  }
  return rows.map((row, index) => {
    const inv = row.inv
    const key = `${inv.parentInvocationId || 'root'}:${inv.serviceId || inv.masterId || inv.invocationId}`
    seen[key] = (seen[key] || 0) + 1
    return { ...row, order: index + 1, repeatIndex: seen[key], repeatTotal: totals[key] }
  })
})

const rootNode = computed(() => traceNodes.value.find(row => row.depth === 0)?.inv || null)
const maxDepth = computed(() => Math.max(0, ...traceNodes.value.map(row => row.depth)))
const succeededCount = computed(() =>
  traceNodes.value.filter(row => row.inv.status === 'SUCCEEDED').length)
const failedCount = computed(() =>
  traceNodes.value.filter(row => row.inv.status === 'FAILED').length)

const short = id => (id || '').slice(0, 14)
const masterOf = inv => mastersById.value[inv.masterId] || null
const serviceOf = inv => servicesById.value[inv.serviceId] || null
const nodeName = inv => {
  const service = serviceOf(inv)
  if (service) return service.display_name || service.slug
  const master = masterOf(inv)
  if (master) return master.display_name || master.slug
  return inv.serviceId ? `未知服务 ${short(inv.serviceId)}` : `未知 Agent ${short(inv.masterId)}`
}
const nodeKind = inv => inv.serviceId ? 'A2A 服务' : '主 Agent'
const nodeSlug = inv => serviceOf(inv)?.slug || masterOf(inv)?.slug || ''
const durationMs = inv => {
  if (!inv?.startedAt || !inv?.finishedAt) return null
  return Math.max(0, new Date(inv.finishedAt) - new Date(inv.startedAt))
}
const duration = inv => {
  const ms = durationMs(inv)
  if (ms == null) return '进行中'
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} 秒` : `${ms} 毫秒`
}
const statusLabel = status => ({
  SUCCEEDED: '成功', FAILED: '失败', RUNNING: '运行中',
  LIMIT_REACHED: '达到轮数上限', REJECTED: '已拒绝', CANCELED: '已取消',
}[status] || status)
const statusClass = status => ({
  SUCCEEDED: 'trace-status-ok', FAILED: 'trace-status-fail', RUNNING: 'trace-status-running',
  LIMIT_REACHED: 'trace-status-warn', REJECTED: 'trace-status-warn', CANCELED: 'trace-status-muted',
}[status] || 'trace-status-muted')
</script>

<template>
  <div class="trace-page">
    <div v-if="canGoBack || trace" class="trace-toolbar">
      <button v-if="canGoBack" class="trace-back" @click="$emit('back')">
        <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9">
          <path d="M19 12H5M11 18l-6-6 6-6"/>
        </svg>
        {{ returnLabel }}
      </button>
      <div v-if="trace" class="trace-current-id" title="当前 Trace ID">
        <span>当前调用链</span>
        <code>{{ trace.traceId }}</code>
      </div>
    </div>

    <section class="trace-head">
      <div class="flex items-end justify-between gap-6 flex-wrap">
        <div>
          <h1>调用链路追踪</h1>
          <p>按业务名称查看主 Agent、服务调用与嵌套回调。序号代表执行顺序，缩进代表调用层级。</p>
        </div>
        <form class="trace-search" @submit.prevent="query">
          <label for="trace-id">输入 Trace ID</label>
          <div class="flex gap-2">
            <input id="trace-id" v-model="traceId" class="input mono" placeholder="trc_…" />
            <button class="btn btn-primary shrink-0" :disabled="loading || !traceId.trim()">
              {{ loading ? '查询中…' : '查询' }}
            </button>
          </div>
        </form>
      </div>
    </section>

    <div v-if="err" class="trace-error">{{ err }}</div>

    <template v-if="trace">
      <section class="trace-summary">
        <div class="trace-summary-main">
          <span class="trace-summary-label">起点</span>
          <strong>{{ rootNode ? nodeName(rootNode) : '未知主 Agent' }}</strong>
          <span v-if="rootNode && nodeSlug(rootNode)" class="mono">{{ nodeSlug(rootNode) }}</span>
        </div>
        <div class="trace-metric">
          <span>总耗时</span><strong>{{ duration(rootNode) }}</strong>
        </div>
        <div class="trace-metric">
          <span>调用节点</span><strong>{{ traceNodes.length }}</strong>
        </div>
        <div class="trace-metric">
          <span>嵌套层级</span><strong>{{ maxDepth + 1 }}</strong>
        </div>
        <div class="trace-metric">
          <span>执行结果</span>
          <strong :class="failedCount ? 'text-brand' : 'text-up'">
            {{ succeededCount }} 成功<template v-if="failedCount"> · {{ failedCount }} 失败</template>
          </strong>
        </div>
      </section>

      <section class="trace-tree-section">
        <div class="trace-section-title">
          <div>
            <span>执行轨道</span>
            <h2>完整调用顺序</h2>
          </div>
          <details>
            <summary>查看追踪技术标识</summary>
            <div class="mono mt-2">trace {{ trace.traceId }}<br />rootTask {{ trace.rootTaskId }}</div>
          </details>
        </div>

        <div v-if="traceNodes.length" class="trace-tree">
          <div v-for="row in traceNodes" :key="row.inv.invocationId"
               class="trace-node-row" :class="{ 'is-child': row.depth > 0 }">
            <span v-if="row.depth > 0" class="trace-elbow"
              :style="{ left: `${row.depth * 38 - 21}px` }"></span>
            <span class="trace-order" :class="row.inv.serviceId ? 'is-service' : 'is-agent'"
              :style="{ left: `${row.depth * 38}px` }">{{ row.order }}</span>

            <article class="trace-node" :class="row.inv.serviceId ? 'is-service' : 'is-agent'"
              :style="{ marginLeft: `${row.depth * 38 + 36}px` }">
              <div class="trace-node-main">
                <div class="min-w-0">
                  <div class="flex items-center gap-2 flex-wrap">
                    <span class="trace-kind">{{ nodeKind(row.inv) }}</span>
                    <span :class="['trace-status', statusClass(row.inv.status)]">
                      {{ statusLabel(row.inv.status) }}
                    </span>
                    <span v-if="row.repeatTotal > 1" class="trace-repeat">
                      第 {{ row.repeatIndex }} 次调用
                    </span>
                  </div>
                  <h3>{{ nodeName(row.inv) }}</h3>
                  <p v-if="nodeSlug(row.inv)" class="mono">{{ nodeSlug(row.inv) }}</p>
                </div>
                <div class="trace-duration">
                  <span>耗时</span><strong>{{ duration(row.inv) }}</strong>
                </div>
              </div>

              <div v-if="row.inv.errorCode" class="trace-node-error">{{ row.inv.errorCode }}</div>

              <details class="trace-tech">
                <summary>技术标识与 {{ row.inv.events.length }} 个事件</summary>
                <div class="mono">
                  invocation {{ row.inv.invocationId }}<br />
                  <template v-if="row.inv.masterId">master {{ row.inv.masterId }}<br /></template>
                  <template v-if="row.inv.serviceId">service {{ row.inv.serviceId }}<br /></template>
                  <template v-if="row.inv.releaseId">release {{ row.inv.releaseId }}<br /></template>
                  {{ row.inv.startedAt }}<template v-if="row.inv.finishedAt"> → {{ row.inv.finishedAt }}</template>
                </div>
              </details>
            </article>
          </div>
        </div>
        <div v-else class="text-t3 text-sm py-8 text-center">这个追踪 ID 没有查到调用记录</div>
      </section>
    </template>

    <!-- 未发起查询时的引导空态，避免页面整片空白 -->
    <div v-else-if="!err" class="card !mb-0 !py-14 flex flex-col items-center text-center">
      <div class="w-12 h-12 rounded-xl bg-canvas text-t3 flex items-center justify-center mb-3">
        <svg class="w-6 h-6" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M3 6h4l4 12h4l4-12"/><circle cx="19" cy="6" r="1.8"/></svg>
      </div>
      <div class="text-t1 font-medium">输入 Trace ID 查看一次调用的完整链路</div>
      <div class="text-t3 mt-1">Trace ID 可在「试运行历史」或调用方日志中获取，形如 trc_…</div>
    </div>
  </div>
</template>

<style scoped>
.trace-page { max-width: 1180px; margin: 0 auto; }
.trace-toolbar {
  position: sticky; top: 10px; z-index: 20; min-height: 46px; margin-bottom: 14px;
  display: flex; align-items: center; gap: 12px; padding: 7px 8px;
  border: 1px solid rgba(229,230,235,.92); border-radius: 12px;
  background: rgba(255,255,255,.93); box-shadow: 0 8px 28px rgba(31,35,41,.09);
  backdrop-filter: blur(12px);
}
.trace-back {
  display: inline-flex; align-items: center; gap: 7px; padding: 7px 11px;
  border-radius: 8px; color: #C22128; font-size: 13px; font-weight: 650;
  cursor: pointer; transition: background .15s ease;
}
.trace-back:hover { background: #FCECEC; }
.trace-current-id { margin-left: auto; display: flex; align-items: center; gap: 9px; padding-right: 8px; }
.trace-current-id span { color: #8F959E; font-size: 11px; }
.trace-current-id code { color: #646A73; font-size: 11px; }
.trace-head {
  padding: 5px 2px 18px; margin-bottom: 18px; border-bottom: 1px solid #E5E6EB;
}
.trace-head h1 { font-size: 21px; line-height: 1.25; font-weight: 680; letter-spacing: -.02em; }
.trace-head p { max-width: 620px; margin-top: 6px; color: #646A73; font-size: 13px; line-height: 1.65; }
.trace-search { width: min(430px, 100%); }
.trace-search label { display: block; margin-bottom: 6px; color: #646A73; font-size: 11px; font-weight: 600; }
.trace-error { margin-bottom: 18px; padding: 13px 16px; border: 1px solid #F3C5C7; border-radius: 10px; color: #C22128; background: #FDF5F5; }
.trace-summary {
  display: grid; grid-template-columns: minmax(250px, 1.7fr) repeat(4, minmax(110px, .7fr));
  margin-bottom: 18px; border: 1px solid #E5E6EB; border-radius: 14px; background: white;
  box-shadow: 0 3px 14px rgba(31,35,41,.05);
}
.trace-summary-main, .trace-metric { min-width: 0; padding: 18px 20px; border-right: 1px solid #ECEDEF; }
.trace-summary > :last-child { border-right: 0; }
.trace-summary-main { display: flex; flex-direction: column; gap: 3px; }
.trace-summary-label, .trace-metric span { color: #8F959E; font-size: 11px; }
.trace-summary-main strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 15px; }
.trace-summary-main .mono { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 11px; }
.trace-metric { display: flex; flex-direction: column; justify-content: center; gap: 5px; }
.trace-metric strong { font-size: 16px; font-weight: 680; }
.trace-tree-section { padding: 24px 26px 28px; border-radius: 16px; background: #EEF0F3; }
.trace-section-title { display: flex; align-items: end; justify-content: space-between; gap: 20px; margin-bottom: 20px; }
.trace-section-title > div > span { color: #C22128; font-size: 11px; font-weight: 650; letter-spacing: .12em; }
.trace-section-title h2 { margin-top: 2px; font-size: 20px; font-weight: 700; }
.trace-section-title details { color: #646A73; font-size: 11px; text-align: right; }
.trace-section-title summary, .trace-tech summary { cursor: pointer; user-select: none; }
.trace-tree { position: relative; }
.trace-node-row { position: relative; min-height: 102px; padding-bottom: 10px; }
.trace-elbow {
  position: absolute; top: -10px; width: 34px; height: 36px;
  border-left: 2px solid #C8CBD0; border-bottom: 2px solid #C8CBD0; border-bottom-left-radius: 10px;
}
.trace-order {
  position: absolute; top: 17px; z-index: 2; width: 27px; height: 27px;
  display: grid; place-items: center; border-radius: 50%; color: white;
  font: 700 11px/1 ui-monospace, monospace; box-shadow: 0 0 0 5px #EEF0F3;
}
.trace-order.is-agent { background: #C22128; }
.trace-order.is-service { background: #505661; }
.trace-node {
  position: relative; padding: 15px 17px 12px; border: 1px solid #DEE0E4; border-radius: 12px;
  background: white; transition: transform .15s ease, box-shadow .15s ease;
}
.trace-node:hover { transform: translateY(-1px); box-shadow: 0 7px 20px rgba(31,35,41,.08); }
.trace-node.is-agent { border-left: 4px solid #C22128; background: #FFFCFC; }
.trace-node-main { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; }
.trace-node h3 { margin-top: 5px; font-size: 15px; font-weight: 680; }
.trace-node p.mono { margin-top: 3px; color: #8F959E; font-size: 11px; }
.trace-kind { color: #8F959E; font-size: 10px; font-weight: 700; letter-spacing: .08em; }
.trace-status, .trace-repeat { padding: 2px 7px; border-radius: 999px; font-size: 10px; font-weight: 650; }
.trace-status-ok { color: #176B3A; background: #E7F6ED; }
.trace-status-fail { color: #C22128; background: #FCECEC; }
.trace-status-running { color: #2459C4; background: #E8F3FF; }
.trace-status-warn { color: #A85A00; background: #FFF3E5; }
.trace-status-muted { color: #646A73; background: #F0F1F3; }
.trace-repeat { color: #A85A00; background: #FFF3E5; }
.trace-duration { flex: 0 0 auto; text-align: right; }
.trace-duration span { display: block; color: #8F959E; font-size: 10px; }
.trace-duration strong { display: block; margin-top: 2px; color: #34383F; font: 650 13px/1.2 ui-monospace, monospace; }
.trace-node-error { margin-top: 9px; padding: 7px 9px; border-radius: 7px; color: #C22128; background: #FCECEC; font-size: 11px; }
.trace-tech { margin-top: 9px; padding-top: 8px; border-top: 1px dashed #E5E6EB; color: #8F959E; font-size: 10px; }
.trace-tech .mono { margin-top: 6px; color: #8F959E; font-size: 10px; line-height: 1.7; word-break: break-all; }
@media (max-width: 900px) {
  .trace-summary { grid-template-columns: repeat(2, 1fr); }
  .trace-summary-main { grid-column: 1 / -1; }
  .trace-summary-main, .trace-metric { border-bottom: 1px solid #ECEDEF; }
  .trace-head { padding-top: 0; }
}
@media (prefers-reduced-motion: reduce) {
  .trace-node, .trace-back { transition: none; }
}
</style>
