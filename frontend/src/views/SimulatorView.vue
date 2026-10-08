<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { config } from '../config.js'
import { getSession } from '../auth.js'
import SchemaInput from '../components/SchemaInput.vue'
import { useServerPagination } from '../pagination.js'
import { readContract, buildSimulationRequest, sendSimulation, reconnectSimulation, simulationTaskRequest, formatSimulationResponse, simulationCurl } from '../simulator.js'

const session = getSession()
const allowed = Boolean(session?.token && (session.kind === 'admin' || session.roles?.includes('eagent_admin')))
const tab = ref('user'), sourceUser = ref(null), target = ref(null), busy = ref(false), loadingCard = ref(false)
const users = ref([]), targets = ref([]), userSearch = ref(''), targetSearch = ref(''), kind = ref('')
const targetGroups = computed(() => [['MASTER', '综合助手'], ['HIAGENT', '专业助手']]
  .map(([targetKind, label]) => ({ kind: targetKind, label, items: targets.value.filter(t => t.kind === targetKind) }))
  .filter(group => group.items.length))
const card = ref(null), contract = ref(null), values = ref({}), editor = ref('form'), jsonDraft = ref('{}')
const text = ref(''), contextId = ref(''), pending = ref(null), messages = ref([]), last = ref(null), attempt = ref(null), error = ref('')
const form = ref(null), optionId = ref(''), copyStatus = ref('')
const runningTaskId = ref('')
let revision = 0
const base = config.apiBase
const usersPager = useServerPagination(users, () => `/api/v1/admin/users?q=${encodeURIComponent(userSearch.value)}&status=active`)
const targetsPager = useServerPagination(targets, () => `/api/v1/admin/simulator/targets?${new URLSearchParams({ asUser: sourceUser.value?.userId || '', q: targetSearch.value, ...(tab.value === 'debug' && kind.value ? { kind: kind.value } : {}) })}`)
const currentValues = computed(() => editor.value === 'json' ? JSON.parse(jsonDraft.value) : values.value)
const preview = computed(() => {
  if (!contract.value) return { error: '请选择用户和助手，加载参数契约' }
  try { return { request: buildSimulationRequest({ contract: contract.value, values: currentValues.value, text: text.value, contextId: contextId.value, pending: pending.value, optionId: optionId.value }) } }
  catch (e) { return { error: e.message } }
})
const resume = computed(() => contract.value?.kind === 'HIAGENT' && pending.value?.inputRequired)
const publicPath = computed(() => target.value ? `/a2a/${encodeURIComponent(target.value.slug)}/rpc` : '')
const publicUrl = computed(() => card.value?.supportedInterfaces?.find(i => i.protocolBinding === 'JSONRPC' && i.protocolVersion === '1.0')?.url || publicPath.value)
const adminPath = computed(() => target.value && sourceUser.value
  ? `/api/v1/admin/simulator/${encodeURIComponent(target.value.slug)}/rpc?${new URLSearchParams({ asUser: sourceUser.value.userId, source: 'debug' })}` : '')
const curl = computed(() => {
  const request = attempt.value || preview.value.request
  return request ? simulationCurl({ url: publicUrl.value, request }) : ''
})
function forwardedRequestJson(request) {
  if (!request || !sourceUser.value || !target.value) return ''
  return JSON.stringify({ method: 'POST', url: publicPath.value,
    headers: { ...request.headers, 'cap-user-name': sourceUser.value.userId }, body: request.body }, null, 2)
}
const requestJson = computed(() => forwardedRequestJson(attempt.value || preview.value.request))
const nextRequestJson = computed(() => forwardedRequestJson(preview.value.request))
const responseJson = computed(() => last.value ? formatSimulationResponse(last.value.raw) : '')
const stateLabel = state => ({ SUCCEEDED: '已完成', FAILED: '执行失败', REJECTED: '已拒绝', INPUT_REQUIRED: '需要补充信息', CANCELED: '已取消', WORKING: '处理中', SUBMITTED: '已提交' })[state] || state

function newConversation() {
  if (busy.value) return
  revision++; contextId.value = ''; pending.value = null; optionId.value = ''; messages.value = []; last.value = null; attempt.value = null; error.value = ''; text.value = ''; copyStatus.value = ''
}
function resetTarget() {
  newConversation(); target.value = null; card.value = null; contract.value = null; values.value = {}; jsonDraft.value = '{}'; editor.value = 'form'; loadingCard.value = false
}
async function loadUsers() {
  try { await usersPager.load({ reset: true, pageSize: 200 }) } catch (e) { error.value = e.message }
}
async function loadTargets() {
  if (!sourceUser.value) return
  try { await targetsPager.load({ reset: true, pageSize: 200 }) } catch (e) { error.value = e.message }
}
function switchTab(next) {
  if (busy.value || next === tab.value) return
  usersPager.invalidate(); targetsPager.invalidate()
  sourceUser.value = null; users.value = []; targets.value = []
  userSearch.value = ''; targetSearch.value = ''; kind.value = ''
  resetTarget(); tab.value = next
  loadUsers()
}
async function chooseUser(event) {
  if (busy.value) return
  resetTarget(); targetsPager.invalidate(); targets.value = []
  sourceUser.value = users.value.find(u => u.userId === event.target.value) || null
  await loadTargets()
}
async function chooseTarget(event) {
  if (busy.value) return
  const selected = targets.value.find(t => t.id === event.target.value)
  resetTarget()
  if (!selected) return
  target.value = selected; loadingCard.value = true
  const current = revision
  try {
    const response = await fetch(base + selected.cardPath, { headers: { Accept: 'application/json' } })
    if (!response.ok) throw new Error(`Agent Card 加载失败（HTTP ${response.status}）`)
    const value = await response.json()
    if (current !== revision) return
    card.value = value; contract.value = readContract(value, selected.kind)
  } catch (e) { if (current === revision) error.value = e.message }
  finally { if (current === revision) loadingCard.value = false }
}
function switchEditor(mode) {
  if (mode === editor.value) return
  try {
    if (mode === 'json') jsonDraft.value = JSON.stringify(values.value, null, 2)
    else {
      const parsed = JSON.parse(jsonDraft.value)
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('参数必须是对象')
      if (contract.value.kind === 'MASTER' && Object.entries(parsed).some(([slug, entry]) => !Object.hasOwn(contract.value.schemas, slug) || !entry || typeof entry !== 'object' || !Object.hasOwn(entry, 'inputs') || !entry.inputs || typeof entry.inputs !== 'object' || Array.isArray(entry.inputs) || Object.keys(entry).length !== 1)) throw new Error('请先修正服务分组：每个已声明服务只包含 inputs 对象')
      values.value = parsed
    }
    editor.value = mode; error.value = ''
  } catch (e) { error.value = e.message }
}
function includeService(slug, enabled) {
  const next = { ...values.value }
  if (enabled) Object.defineProperty(next, slug, { value: { inputs: {} }, enumerable: true, configurable: true, writable: true })
  else delete next[slug]
  values.value = next
}
function updateService(slug, inputs) { values.value = { ...values.value, [slug]: { inputs } } }
async function send() {
  if (busy.value || !target.value || !sourceUser.value || !contract.value) return
  if (!form.value.reportValidity()) return
  if (!preview.value.request) { error.value = preview.value.error; return }
  const request = preview.value.request
  const current = revision
  attempt.value = JSON.parse(JSON.stringify(request)); last.value = null; error.value = ''; copyStatus.value = ''; busy.value = true
  const userText = resume.value ? pending.value.interaction.options.find(o => o.id === optionId.value)?.label : text.value
  messages.value.push({ role: 'user', text: userText })
  const replyIndex = messages.value.length
  messages.value.push({ role: 'agent', text: '', status: stateLabel('SUBMITTED'), cards: [] })
  try {
    const response = await sendSimulation({ base, slug: target.value.slug, asUser: sourceUser.value.userId, source: tab.value, token: getSession()?.token, request,
      onProgress: update => { if (current === revision) { runningTaskId.value = update.taskId || runningTaskId.value; messages.value[replyIndex] = { role: 'agent', text: update.output || '', status: stateLabel(update.status), cards: update.cards } } } })
    if (current !== revision) return
    last.value = response
    if (response.error) { error.value = response.error; messages.value.splice(replyIndex, 1, { role: 'error', text: response.error }); return }
    const task = response.task
    contextId.value = task.contextId || contextId.value
    pending.value = task; optionId.value = ''
    messages.value[replyIndex] = { role: 'agent', text: task.output || stateLabel(task.status), status: stateLabel(task.status), cards: task.cards }
    if (!['FAILED', 'REJECTED', 'CANCELED'].includes(task.status)) text.value = ''
    else error.value = task.errorDetail || task.output || stateLabel(task.status)
  } catch (e) {
    if (current !== revision) return
    if (runningTaskId.value) {
      try {
        const task = await reconnectSimulation({ base, slug: target.value.slug,
          asUser: sourceUser.value.userId, source: tab.value, token: getSession()?.token },
        runningTaskId.value, update => { messages.value[replyIndex] = { role: 'agent', text: update.output || '', status: stateLabel(update.status), cards: update.cards } })
        messages.value[replyIndex] = { role: 'agent', text: task.output || stateLabel(task.status), status: stateLabel(task.status), cards: task.cards }
        contextId.value = task.contextId || contextId.value
        pending.value = task
        return
      } catch (reconnectError) { error.value = `${e.message}；重连失败：${reconnectError.message}` }
    } else error.value = `${e.message}。未自动重试，请确认调用结果后再操作。`
    messages.value.splice(replyIndex, 1, { role: 'error', text: error.value })
  } finally { if (current === revision) { busy.value = false; runningTaskId.value = '' } }
}
async function stopTask() {
  if (!runningTaskId.value || !target.value || !sourceUser.value) return
  try {
    await sendSimulation({ base, slug: target.value.slug, asUser: sourceUser.value.userId,
      source: tab.value, token: getSession()?.token,
      request: simulationTaskRequest('CancelTask', runningTaskId.value) })
  } catch (e) { error.value = e.message }
}
async function copyText(value, label) {
  if (!value) return
  try { await navigator.clipboard.writeText(value); copyStatus.value = `${label}已复制` }
  catch { copyStatus.value = `无法访问剪贴板，请手动复制${label}` }
}
const copyCurl = () => copyText(curl.value, 'curl 示例')
const copyRequest = () => copyText(requestJson.value, '请求')
const copyResponse = () => copyText(responseJson.value, '响应')
watch(kind, () => { if (tab.value === 'debug' && !busy.value) { resetTarget(); loadTargets() } })
onMounted(() => { if (allowed) loadUsers() })
onBeforeUnmount(() => { revision++; usersPager.invalidate(); targetsPager.invalidate() })
</script>

<template>
  <section v-if="!allowed" class="card p-6">模拟中心需要有效管理员 Session，请重新登录。</section>
  <section v-else class="simulator">
    <header class="flex flex-wrap justify-between gap-3 items-start mb-5"><div><h1 class="text-xl font-semibold">Agent 模拟中心</h1><p class="text-sm text-t3 mt-1">操作管理员：{{ session.displayName || session.userId }} · 当前模拟用户：{{ sourceUser?.displayName || sourceUser?.userId || '未选择' }}</p></div><span class="badge badge-warn">真实调用 · 将执行实际 Agent 能力</span></header>
    <div class="flex border-b border-line gap-6 mb-5" role="tablist" aria-label="模拟方式"><button v-for="item in [['user','用户模拟'],['debug','A2A 协议调试']]" :key="item[0]" type="button" role="tab" :aria-selected="tab === item[0]" :class="['pb-3 border-b-2',tab===item[0]?'border-brand text-brand':'border-transparent text-t3']" @click="switchTab(item[0])">{{ item[1] }}</button></div>
    <fieldset :disabled="busy" class="card p-4 mb-4 grid md:grid-cols-2 gap-5">
      <div><label class="text-sm font-medium block mb-2" for="sim-user-search">模拟用户（已登记且启用）</label><div class="flex gap-2"><input id="sim-user-search" v-model="userSearch" class="input min-w-0" placeholder="搜索姓名或工号" @keyup.enter="loadUsers"><button class="btn btn-ghost" @click="loadUsers">查询</button></div><select aria-label="选择模拟用户" class="input w-full mt-2" :value="sourceUser?.userId || ''" @change="chooseUser"><option value="">请选择用户</option><option v-if="sourceUser && !users.some(u => u.userId===sourceUser.userId)" :value="sourceUser.userId">{{ sourceUser.displayName }}（{{ sourceUser.userId }}）</option><option v-for="u in users.filter(u=>u.status==='active')" :key="u.userId" :value="u.userId">{{ u.displayName || u.userId }}（{{ u.userId }}）</option></select></div>
      <div><div class="flex items-center justify-between mb-2"><label class="text-sm font-medium" for="sim-target-search">{{ tab==='user'?'选择智能助手':'调用目标' }}</label><select v-show="tab==='debug'" v-model="kind" class="input !w-auto" aria-label="调用方式"><option value="">全部类型</option><option value="MASTER">调用主 Agent</option><option value="HIAGENT">直连 HiAgent</option></select></div><div class="flex gap-2"><input id="sim-target-search" v-model="targetSearch" class="input min-w-0" placeholder="搜索助手" :disabled="!sourceUser" @keyup.enter="loadTargets"><button class="btn btn-ghost" :disabled="!sourceUser" @click="loadTargets">查询</button></div><select aria-label="选择智能助手" class="input w-full mt-2" :value="target?.id || ''" :disabled="!sourceUser" @change="chooseTarget"><option value="">请选择助手</option><option v-if="target && !targets.some(t=>t.id===target.id)" :value="target.id">{{ target.name }}</option><optgroup v-for="group in targetGroups" :key="group.kind" :label="group.label"><option v-for="t in group.items" :key="t.id" :value="t.id" :disabled="!t.available">{{ t.name }}{{ t.available?'':` · ${t.unavailableReason}` }}</option></optgroup></select></div>
    </fieldset>
    <p v-if="loadingCard" class="text-t3 p-4">正在读取助手参数…</p>
    <p v-if="error" role="alert" class="text-brand border border-brand rounded-lg p-3 mb-4 break-words">{{ error }}</p>
    <p v-if="!target && !loadingCard" class="card p-10 text-center text-t3">先选择模拟用户，再选择智能助手。</p>
    <form v-if="target && contract" ref="form" @submit.prevent="send">
      <div class="sim-grid">
        <div class="card min-w-0 flex flex-col">
          <div class="p-4 border-b border-line flex justify-between items-center gap-3"><div><h2 class="font-semibold">{{ target.name }}</h2><span class="text-xs text-t3">{{ target.kind==='MASTER'?'综合助手 · 成员能力按模拟用户权限提供':'专业助手 · 直接处理你的请求' }}</span></div><button type="button" class="btn btn-ghost" :disabled="busy" @click="newConversation">新对话</button></div>
          <div class="sim-chat" aria-live="polite"><p v-if="!messages.length" class="text-t3 text-sm">填写本次业务信息，开始一段对话。标记 * 的字段需要提供。</p><div v-for="(message,i) in messages" :key="i" :class="['sim-bubble',message.role==='user'?'sim-self':'',message.role==='error'?'text-brand':'']"><div class="text-xs opacity-70 mb-1">{{ message.role==='user'?'模拟用户':message.role==='error'?'调用错误':target.name }} {{ message.status }}</div><p class="whitespace-pre-wrap break-words">{{ message.text }}</p><details v-for="(c,n) in message.cards || []" :key="n" class="mt-2"><summary class="cursor-pointer">业务卡片详情 {{ n+1 }}</summary><pre class="sim-pre">{{ typeof c.payload==='string'?c.payload:JSON.stringify(c.payload,null,2) }}</pre></details></div><p v-if="busy" class="text-t3 text-sm">正在处理…</p></div>
          <fieldset :disabled="busy" class="p-4 border-t border-line">
            <div v-if="resume" class="mb-3"><p class="text-sm mb-2">请更新本轮业务信息并选择一项：</p><button v-for="option in pending.interaction?.options || []" :key="option.id" type="button" :aria-pressed="optionId===option.id" :class="['btn mr-2 mb-2', optionId===option.id?'btn-primary':'btn-ghost']" @click="optionId=option.id">{{ option.label }}</button><p v-if="!pending.interaction?.options?.length" class="text-brand text-sm">暂不支持该交互形式，请新建对话。</p></div>
            <textarea v-model="text" aria-label="用户消息" class="input w-full" rows="3" placeholder="输入你想咨询的问题…" :disabled="resume" />
            <div class="flex justify-between items-center gap-2 mt-2"><span class="text-xs text-t3">业务信息在本次对话中保留，每轮完整发送</span><div class="flex gap-2"><button v-if="busy && runningTaskId" class="btn btn-ghost" type="button" @click="stopTask">停止任务</button><button class="btn btn-primary" type="submit" :disabled="!preview.request || busy">{{ busy?'执行中…':resume?'提交选项':'发送' }}</button></div></div>
          </fieldset>
        </div>
        <fieldset :disabled="busy" class="card p-4 min-w-0 self-start"><div class="flex justify-between gap-3 items-center mb-4"><h2 class="font-semibold">本次业务信息</h2><div class="flex gap-2 text-sm"><button type="button" :class="editor==='form'?'text-brand':'text-t3'" @click="switchEditor('form')">表单</button><button type="button" :class="editor==='json'?'text-brand':'text-t3'" @click="switchEditor('json')">JSON</button></div></div>
          <template v-if="editor==='form'"><template v-if="contract.kind==='MASTER'"><p class="text-xs text-t3 mb-3">勾选表示提供该助手的业务信息，不会强制主助手调用它。</p><div v-for="(schema,slug) in contract.schemas" :key="slug" class="border-t border-line py-3"><label class="flex gap-2 mb-3 text-sm"><input type="checkbox" :checked="Object.hasOwn(values,slug)" @change="includeService(slug,$event.target.checked)">{{ schema.title || slug }} · 提供信息</label><SchemaInput v-if="Object.hasOwn(values,slug)" :schema="schema" :model-value="values[slug].inputs" @update:model-value="updateService(slug,$event)" /></div><p v-if="!Object.keys(contract.schemas).length" class="text-sm text-t3">此助手没有声明额外业务参数。</p></template><SchemaInput v-else v-model="values" :schema="contract.schemas.inputs" /></template>
          <template v-else><label class="text-xs text-t3" for="sim-json">{{ contract.kind==='MASTER'?'各服务的 inputs 对象':'完整业务 Inputs 对象' }}</label><textarea id="sim-json" v-model="jsonDraft" rows="14" class="input w-full font-mono mt-2" /></template>
          <p v-if="preview.error" class="text-xs text-t3 mt-4 whitespace-pre-wrap">{{ preview.error }}</p>
        </fieldset>
      </div>
      <section v-if="tab==='debug'" class="card p-4 mt-4 min-w-0"><div class="flex flex-wrap justify-between items-center gap-3 mb-3"><h2 class="font-semibold">协议与调用详情</h2><button class="btn btn-ghost" type="button" :disabled="!curl" @click="copyCurl">复制 curl 示例</button></div><p class="text-xs text-t3 break-all">调用类型：{{ target.kind==='MASTER'?'调用主 Agent':'直连 HiAgent' }}<br>公开接口：{{ publicUrl }}<br>管理员代调用入口：{{ adminPath }}<br>扩展：{{ contract.uri }}<br>Context：{{ contextId || '新对话' }} · Task：{{ pending?.taskId || '无' }}</p><p class="text-xs text-t3 mt-2">请求区展示后台转发给 Agent 的视图；所选工号由后台校验后注入 cap-user-name，浏览器只通过 asUser 指定模拟用户。</p><p class="text-xs text-t3 mt-2">{{ copyStatus }}</p>
        <div class="grid lg:grid-cols-2 gap-4 mt-4"><div><div class="flex justify-between items-center gap-2 mb-2"><h3 class="text-sm">{{ attempt?'最近一次请求（后台转发视图）':'请求预览（后台转发视图）' }}</h3><button class="btn btn-ghost text-xs" type="button" :disabled="!requestJson" @click="copyRequest">复制请求</button></div><pre class="sim-pre">{{ requestJson || preview.error }}</pre><details v-if="attempt" class="mt-2"><summary>下一轮预览</summary><pre class="sim-pre">{{ nextRequestJson || preview.error }}</pre></details></div><div><div class="flex justify-between items-center gap-2 mb-2"><h3 class="text-sm">响应 {{ last ? `· HTTP ${last.httpStatus} · ${last.elapsedMs} ms` : '' }}</h3><button class="btn btn-ghost text-xs" type="button" :disabled="!responseJson" @click="copyResponse">复制响应</button></div><pre class="sim-pre">{{ last ? (responseJson || '响应为空') : '尚未调用' }}</pre><p v-if="last && last.envelope === undefined && last.raw" class="text-xs text-t3 mt-2">响应不是 JSON，已保留原文。</p><p class="text-xs text-t3 break-all mt-2">Trace：{{ last?.traceId || '—' }} · 审计关联：{{ last?.simulationId || '—' }}</p></div></div>
        <details class="mt-4"><summary>Agent Card 与参数 Schema</summary><pre class="sim-pre mt-2">{{ JSON.stringify(card,null,2) }}</pre></details><details class="mt-3"><summary>脱敏 curl 接入示例</summary><pre class="sim-pre mt-2">{{ curl }}</pre></details>
      </section>
      <p v-if="last?.auditWarning" role="alert" class="text-brand mt-3">调用已返回，但完成审计写入失败。请按审计关联 ID 排查，请勿因此重复执行。</p>
    </form>
  </section>
</template>

<style scoped>
.sim-grid { display:grid; grid-template-columns:minmax(0,1.35fr) minmax(280px,1fr); gap:16px; align-items:start }
.sim-chat { padding:20px; min-height:300px; display:flex; flex-direction:column; gap:14px }
.sim-bubble { background:#f4f6fa; border:1px solid #e5e7eb; border-radius:12px; padding:12px; max-width:94%; font-size:14px }
.sim-self { margin-left:auto; background:#fdf0f1 }
.sim-pre { background:#f5f7fa; padding:12px; border-radius:8px; font-size:12px; white-space:pre-wrap; overflow-wrap:anywhere; max-height:420px; overflow:auto }
@media(max-width:1050px) { .sim-grid { grid-template-columns:1fr } }
</style>
