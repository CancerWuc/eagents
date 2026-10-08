<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api.js'
import { fmtDateTime } from '../format.js'
import Pagination from '../components/Pagination.vue'
import { useServerPagination } from '../pagination.js'

defineProps({ canManage: { type: Boolean, default: false } })

const workspace = ref('default-workspace')
const items = ref([])
const itemsPager = useServerPagination(items, () => `/api/v1/workspaces/${encodeURIComponent(workspace.value.trim())}/installations`)
const err = ref('')
const services = ref([])
const releases = ref([])
const form = ref({ serviceId: '', releaseId: '' })
const showForm = ref(false)

async function load() {
  try {
    await itemsPager.load()
    err.value = ''
  } catch (e) { err.value = e.message }
}
async function loadServices() {
  try {
    const result = await api.serviceSelector('installation')
    services.value = result.items.map(s => ({ service_id: s.serviceId, slug: s.slug, display_name: s.displayName }))
  } catch (e) { err.value = e.message }
}
onMounted(() => { load(); loadServices() })

async function pickService() {
  releases.value = []
  form.value.releaseId = ''
  if (!form.value.serviceId) return
  try {
    const result = await api.releaseSelector('installation', form.value.serviceId)
    releases.value = result.items.map(r => ({ release_id: r.releaseId, agent_version: r.agentVersion, state: r.state }))
  } catch (e) { /* 通知 */ }
}

async function install() {
  try {
    await api.installRelease(workspace.value.trim(), {
      serviceId: form.value.serviceId,
      releaseId: form.value.releaseId,
      acceptedPermissions: ['invoke'],
    })
    showForm.value = false
    await load()
  } catch (e) { /* 通知已展示 */ }
}

async function setSt(i, status) {
  try { await api.patchInstallation(i.installation_id, status); await load() } catch (e) { /* 通知 */ }
}
async function uninstall(i) {
  try { await api.uninstallInstallation(i.installation_id); await load() } catch (e) { /* 通知 */ }
}

// 状态中文标签（原枚举保留在 title 提示中）
const STATUS_TEXT = { ENABLED: '已启用', DISABLED: '已停用', UNINSTALLED: '已卸载' }
const RELEASE_STATE_TEXT = { DRAFT: '草稿', PUBLISHED: '已发布', SUSPENDED: '已暂停', REVOKED: '已撤销' }

const statusClass = (s) => ({
  ENABLED: 'bg-green-100 text-green-700', DISABLED: 'bg-amber-100 text-amber-700',
  UNINSTALLED: 'bg-canvas text-t3',
}[s] || 'bg-canvas text-t2')
</script>

<template>
  <div>
    <div class="flex items-start justify-between mb-5">
      <div>
        <h1 class="text-xl font-semibold">工作空间安装</h1>
        <p class="text-t2 mt-1">把某个已发布的版本装进工作空间，装了才能被调用。卸载只是停用，历史记录保留；运行时不会自动回退到旧版本。</p>
      </div>
      <button v-if="canManage" class="btn btn-primary" :disabled="showForm" @click="showForm = true; pickService()">+ 安装版本</button>
    </div>

    <div v-if="err" class="card text-brand mb-4">{{ err }}</div>

    <div class="card mb-5 flex items-center gap-3">
      <span class="text-sm text-t2">工作空间</span>
      <input v-model="workspace" class="input !w-72" placeholder="default-workspace" @keyup.enter="load" />
      <button class="btn btn-ghost" @click="load">查询</button>
    </div>

    <div v-if="showForm" class="card mb-5">
      <div class="flex items-end gap-3">
        <div>
          <div class="text-xs text-t3 mb-1">服务名称 / 标识</div>
          <select v-model="form.serviceId" class="input !w-64" @change="pickService">
            <option value="" disabled>选择服务</option>
            <option v-for="s in services" :key="s.service_id" :value="s.service_id">{{ s.display_name }}（标识：{{ s.slug }}）</option>
          </select>
        </div>
        <div>
          <div class="text-xs text-t3 mb-1">选择版本（只有「已发布」状态的能装上）</div>
          <select v-model="form.releaseId" class="input !w-72">
            <option value="" disabled>选择版本</option>
            <option v-for="r in releases" :key="r.release_id" :value="r.release_id">
              {{ r.agent_version }} · {{ RELEASE_STATE_TEXT[r.state] || r.state }}
            </option>
          </select>
        </div>
        <button class="btn btn-primary" :disabled="!form.releaseId" @click="install">安装</button>
        <button class="btn btn-ghost" @click="showForm = false">取消</button>
        <span v-if="!form.releaseId" class="text-xs text-t3 self-center">
          {{ releases.length ? '先选一个版本' : '该服务还没有版本，请先去「版本发布」创建并发布' }}
        </span>
      </div>
    </div>

    <div class="card !p-0 overflow-hidden">
      <table class="tbl">
        <thead><tr><th>服务标识</th><th>版本号</th><th>版本状态</th><th>安装状态</th><th>安装时间</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="i in items" :key="i.installation_id">
            <td class="font-medium">{{ i.service_slug }}</td>
            <td>{{ i.agent_version }}</td>
            <td><span :class="`px-2 py-0.5 rounded text-xs ${i.release_state === 'PUBLISHED' ? 'bg-green-100 text-green-700' : 'bg-amber-100 text-amber-700'}`" :title="i.release_state">{{ RELEASE_STATE_TEXT[i.release_state] || i.release_state }}</span></td>
            <td><span :class="`px-2 py-0.5 rounded text-xs font-medium ${statusClass(i.status)}`" :title="i.status">{{ STATUS_TEXT[i.status] || i.status }}</span></td>
            <td class="text-xs text-t2 whitespace-nowrap">{{ fmtDateTime(i.created_at) }}</td>
            <td>
              <div class="flex gap-1.5">
                <button v-if="canManage && i.status === 'DISABLED'" class="btn btn-text text-green-700" @click="setSt(i, 'ENABLED')">启用</button>
                <button v-if="canManage && i.status === 'ENABLED'" class="btn btn-text text-amber-700" @click="setSt(i, 'DISABLED')">禁用</button>
                <button v-if="canManage && i.status !== 'UNINSTALLED'" class="btn btn-text text-red-600" @click="uninstall(i)">卸载</button>
              </div>
            </td>
          </tr>
          <tr v-if="!items.length"><td colspan="6" class="text-center text-t3 py-8">这个工作空间还没装任何版本</td></tr>
        </tbody>
      </table>
      <Pagination :pager="itemsPager" />
    </div>
  </div>
</template>
