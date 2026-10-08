<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api.js'
import { promptText } from '../ui.js'
import { fmtDateTime } from '../format.js'
import Pagination from '../components/Pagination.vue'
import { useServerPagination } from '../pagination.js'

defineProps({ canManage: { type: Boolean, default: false } })

const services = ref([])
const selected = ref('')
const releases = ref([])
const releasesPager = useServerPagination(releases, () => `/api/v1/services/${selected.value}/releases`)
const err = ref('')

async function loadServices() {
  try {
    const result = await api.serviceSelector('release')
    services.value = result.items.map(s => ({ service_id: s.serviceId, slug: s.slug, display_name: s.displayName }))
    if (!selected.value && services.value.length) selected.value = services.value[0].service_id
    if (selected.value) await load()
    err.value = ''
  } catch (e) { err.value = e.message }
}
onMounted(loadServices)

async function load() {
  if (!selected.value) return
  try { await releasesPager.load(); err.value = '' }
  catch (e) { err.value = e.message }
}

async function create() {
  const version = await promptText('版本号（留空则使用服务 Card 里声明的版本）', '')
  // 取消返回 null，留空提交返回 ''——本场景「留空」是合法输入（沿用 Card
  // 版本），两者必须区分。此前统一按 falsy 处理，点取消照样发创建请求。
  if (version === null) return
  try {
    await api.createRelease(
      selected.value, version.trim() ? { agentVersion: version.trim() } : {},
    )
    await load()
  } catch (e) { /* 全局通知已展示 code/traceId */ }
}


async function act(fn, id) {
  try { await fn(id); await load() } catch (e) { /* 通知已展示 */ }
}

// 状态中文标签（原枚举值保留在 title 提示中，便于对接口排障）
const STATE_TEXT = { DRAFT: '草稿', PUBLISHED: '已发布', SUSPENDED: '已暂停', REVOKED: '已撤销' }
const stateText = (s) => STATE_TEXT[s] || s

const stateClass = (s) => ({
  DRAFT: 'bg-canvas text-t2', PUBLISHED: 'bg-green-100 text-green-700',
  SUSPENDED: 'bg-amber-100 text-amber-700', REVOKED: 'bg-red-100 text-red-600',
}[s] || 'bg-canvas text-t2')

const svcName = (id) => {
  const s = services.value.find((x) => x.service_id === id)
  return s ? s.slug : id
}
</script>

<template>
  <div>
    <div class="flex items-start justify-between mb-5">
      <div>
        <h1 class="text-xl font-semibold">版本发布</h1>
        <p class="text-t2 mt-1">给注册的服务打一个不可修改的版本快照。流转：草稿 → 已发布 ⇄ 已暂停 → 已撤销。<span class="text-t3">只有「已发布」且已在工作空间安装的版本，运行时才会被调用。</span></p>
      </div>
    </div>

    <div v-if="err" class="card text-brand mb-4">{{ err }}</div>

    <!-- 动作与它依赖的选择器同处一行：原先按钮在页头，选服务在下方卡片，
         中间隔着描述文字，「为什么按钮是灰的」无从判断 -->
    <div class="card mb-5 flex items-center gap-3">
      <span class="text-sm text-t2">服务名称 / 标识</span>
      <select v-model="selected" class="input !w-72" @change="load">
        <option v-for="s in services" :key="s.service_id" :value="s.service_id">
          {{ s.display_name }}（标识：{{ s.slug }}）
        </option>
      </select>
      <button v-if="canManage" class="btn btn-primary ml-auto" :disabled="!selected" @click="create">+ 新建草稿版本</button>
      <span v-if="!selected" class="text-xs text-t3">先选一个服务</span>
    </div>

    <div class="card !p-0 overflow-hidden">
      <table class="tbl">
        <thead><tr><th>版本号</th><th>状态</th><th>服务地址</th><th>内容摘要</th><th>发布时间</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="r in releases" :key="r.release_id">
            <td class="font-medium">{{ r.agent_version }}</td>
            <td><span :class="`px-2 py-0.5 rounded text-xs font-medium ${stateClass(r.state)}`" :title="r.state">{{ stateText(r.state) }}</span></td>
            <td class="text-xs text-t2 max-w-56 truncate">{{ r.endpoint }}</td>
            <td class="text-xs text-t3 font-mono">{{ (r.card_digest || '').slice(0, 10) }}…</td>
            <td class="text-xs text-t2 whitespace-nowrap">{{ fmtDateTime(r.published_at) }}</td>
            <td>
              <div class="flex gap-1.5">
                <button v-if="canManage && (r.state === 'DRAFT' || r.state === 'SUSPENDED')"
                        class="btn btn-text text-green-700" @click="act(api.publishRelease, r.release_id)">发布</button>
                <button v-if="canManage && r.state === 'PUBLISHED'"
                        class="btn btn-text text-amber-700" @click="act(api.suspendRelease, r.release_id)">暂停</button>
                <button v-if="canManage && r.state !== 'REVOKED'"
                        class="btn btn-text text-red-600" @click="act(api.revokeRelease, r.release_id)">撤销</button>
              </div>
            </td>
          </tr>
          <tr v-if="!releases.length"><td colspan="6" class="text-center text-t3 py-8">该服务还没有任何版本。点右上角「按当前服务信息建一个草稿版本」开始</td></tr>
        </tbody>
      </table>
      <Pagination :pager="releasesPager" />
    </div>
  </div>
</template>
