<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api.js'
import Pagination from '../components/Pagination.vue'
import { useServerPagination } from '../pagination.js'

defineProps({ canManage: { type: Boolean, default: false } })

const services = ref([])
const selected = ref('')
const instances = ref([])
const instancesPager = useServerPagination(instances, () => `/api/v1/services/${selected.value}/instances`)
const err = ref('')
const showForm = ref(false)
const form = ref({ instanceName: '', endpoint: '', leaseTtlSeconds: 30 })

async function loadServices() {
  try {
    const result = await api.serviceSelector('instance')
    services.value = result.items.map(s => ({ service_id: s.serviceId, slug: s.slug, display_name: s.displayName }))
    if (!selected.value && services.value.length) selected.value = services.value[0].service_id
    if (selected.value) await load()
    err.value = ''
  } catch (e) { err.value = e.message }
}
onMounted(loadServices)

async function load() {
  if (!selected.value) return
  try { await instancesPager.load(); err.value = '' }
  catch (e) { err.value = e.message }
}

async function register() {
  try {
    await api.registerInstance({
      serviceId: selected.value,
      instanceName: form.value.instanceName,
      endpoint: form.value.endpoint,
      leaseTtlSeconds: Number(form.value.leaseTtlSeconds) || 30,
    })
    showForm.value = false
    form.value = { instanceName: '', endpoint: '', leaseTtlSeconds: 30 }
    await load()
  } catch (e) { /* 通知已展示 */ }
}

async function beat(i) {
  try { await api.heartbeatInstance(i.instanceId); await load() } catch (e) { /* 通知 */ }
}
async function dereg(i) {
  try { await api.deregisterInstance(i.instanceId); await load() } catch (e) { /* 通知 */ }
}

// 状态中文标签（原枚举保留在 title 提示中）
const STATE_TEXT = { READY: '可用', DRAINING: '摘流中', UNAVAILABLE: '不可用', UNKNOWN: '未知' }

const stateClass = (s) => ({
  READY: 'bg-green-100 text-green-700', DRAINING: 'bg-amber-100 text-amber-700',
  UNAVAILABLE: 'bg-red-100 text-red-600', UNKNOWN: 'bg-canvas text-t2',
}[s] || 'bg-canvas text-t2')
</script>

<template>
  <div>
    <div class="flex items-start justify-between mb-5">
      <div>
        <h1 class="text-xl font-semibold">服务实例</h1>
        <p class="text-t2 mt-1">一个服务可以跑多个副本（实例），每个副本定期发心跳续租。<span class="text-t3">注册过实例的服务会按租约路由到这些地址；只有「可用」状态的实例才会收到流量。</span></p>
      </div>
    </div>

    <div v-if="err" class="card text-brand mb-4">{{ err }}</div>

    <div class="card mb-5 flex items-center gap-3">
      <span class="text-sm text-t2">服务名称 / 标识</span>
      <select v-model="selected" class="input !w-72" @change="load">
        <option v-for="s in services" :key="s.service_id" :value="s.service_id">{{ s.display_name }}（标识：{{ s.slug }}）</option>
      </select>
      <button v-if="canManage" class="btn btn-primary ml-auto" :disabled="!selected || showForm" @click="showForm = true">+ 注册实例</button>
      <span v-if="!selected" class="text-xs text-t3">先选一个服务</span>
    </div>

    <div v-if="showForm" class="card mb-5">
      <div class="flex items-end gap-3 flex-wrap">
        <div>
          <div class="text-xs text-t3 mb-1">实例名</div>
          <input v-model="form.instanceName" class="input !w-48" placeholder="weather-1" />
        </div>
        <div>
          <div class="text-xs text-t3 mb-1">endpoint</div>
          <input v-model="form.endpoint" class="input !w-72 font-mono" placeholder="http://10.0.0.1:9001" />
        </div>
        <div>
          <div class="text-xs text-t3 mb-1">租约时长（秒，到期未续约即摘除）</div>
          <input v-model="form.leaseTtlSeconds" class="input !w-24" type="number" min="5" max="600" />
        </div>
        <button class="btn btn-primary" :disabled="!form.instanceName || !form.endpoint" @click="register">注册</button>
        <button class="btn btn-ghost" @click="showForm = false">取消</button>
      </div>
    </div>

    <div class="card !p-0 overflow-hidden">
      <table class="tbl">
        <thead><tr><th>实例名</th><th>实例地址</th><th>状态</th><th>可接流量</th><th>租约到期</th><th>最近心跳</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="i in instances" :key="i.instanceId">
            <td class="font-medium">{{ i.instanceName }}</td>
            <td class="text-xs font-mono text-t2 max-w-56 truncate">{{ i.endpoint }}</td>
            <td><span :class="`px-2 py-0.5 rounded text-xs font-medium ${stateClass(i.state)}`" :title="i.state">{{ STATE_TEXT[i.state] || i.state }}</span></td>
            <td class="text-xs">{{ i.ready ? '✓' : '—' }}</td>
            <td class="text-xs text-t2">{{ i.leaseExpiresAt || '—' }}</td>
            <td class="text-xs text-t2">{{ i.lastHeartbeatAt || '—' }}</td>
            <td>
              <div class="flex gap-1.5">
                <button v-if="canManage" class="btn btn-text" :disabled="i.terminating" @click="beat(i)">心跳</button>
                <button v-if="canManage && !i.terminating" class="btn btn-text text-red-600" @click="dereg(i)">注销</button>
              </div>
            </td>
          </tr>
          <tr v-if="!instances.length"><td colspan="7" class="text-center text-t3 py-8">该服务没有注册实例，调用会直接走版本里登记的服务地址</td></tr>
        </tbody>
      </table>
      <Pagination :pager="instancesPager" />
    </div>
  </div>
</template>
