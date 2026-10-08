<script setup>
import { onBeforeUnmount, onMounted, ref, computed } from 'vue'
import TeamView from './views/TeamView.vue'
import RegistryView from './views/RegistryView.vue'
import MasterDetailView from './views/MasterDetailView.vue'
import PermissionView from './views/PermissionView.vue'
import LoopEngineerView from './views/LoopEngineerView.vue'
import ReleaseView from './views/ReleaseView.vue'
import InstallationView from './views/InstallationView.vue'
import TraceView from './views/TraceView.vue'
import InstanceView from './views/InstanceView.vue'
import KbView from './views/KbView.vue'
import PerformanceView from './views/PerformanceView.vue'
import GatewayView from './views/GatewayView.vue'
import EnhancementView from './views/EnhancementView.vue'
import MyPermissionView from './views/MyPermissionView.vue'
import SimulatorView from './views/SimulatorView.vue'
import LoginView from './components/LoginView.vue'
import { api } from './api.js'
import { hasAuth, getSession, logout, isAdmin } from './auth.js'
import { pageAccess, refreshPageAccess, canViewPage, canManagePage, clearPageAccess } from './page-permissions.js'
import { onModalConfirm, onModalPrompt } from './ui.js'
import { toasts, dismiss, notify } from './notify.js'
import { config } from './config.js'

const view = ref(isAdmin() ? 'multi' : 'me')
const simulatorNavOpen = ref(false)
const detailMasterId = ref(null)
const detailInitialTab = ref('bindings')
// 登录门禁（admin-auth 01）：无任何凭据 → 登录页
const admin = ref(isAdmin())  // permission-control v1：治理页仅管理员可见
const authed = ref(hasAuth())
const sessionUser = ref(getSession()?.displayName || '')
async function onLoggedIn() {
  sessionUser.value = getSession()?.displayName || ''
  admin.value = isAdmin()
  authed.value = true
  await loadAccess(true)
}
async function onLogout() {
  await logout()
  clearPageAccess()
  window.location.reload()
}
const traceQueryId = ref('')
const traceReturn = ref(null)
const crumb = computed(() => ({
  multi: 'Agent Team',
  registry: '服务注册',
  master: 'Agent Team 详情',
  permission: '权限管理',
  loop: '提示词调优',
  release: '版本发布',
  installation: '工作空间安装',
  trace: '调用链路追踪',
  instance: '服务实例',
  kb: '知识库',
  performance: '性能测试',
  gateway: '网关配置',
  enhance: '平台增强',
  simulator: 'Agent 模拟中心',
}[view.value]))
const appName = config.appName

function nav(v) {
  if (!canViewPage(v)) {
    notify({ code: 'FORBIDDEN', message: '没有该页面的查看权限，请联系管理员开通' })
    return
  }
  // 主动从侧边栏进出 Trace 视为一次全新查看；只有结果面板的 openTrace
  // 才携带要自动查询的 ID 和返回位置。
  if (v === 'trace' || (view.value === 'trace' && v !== 'trace')) {
    traceQueryId.value = ''
    traceReturn.value = null
  }
  view.value = v
  simulatorNavOpen.value = false
}
function openMaster(id) {
  if (!canViewPage('multi')) return nav('multi')
  detailMasterId.value = id
  detailInitialTab.value = 'bindings'
  view.value = 'master'
}
function openTrace(payload) {
  if (!canViewPage('trace')) {
    notify({ code: 'FORBIDDEN', message: '没有调用链路追踪页面权限' })
    return
  }
  const request = typeof payload === 'string' ? { traceId: payload } : payload
  traceQueryId.value = request.traceId
  traceReturn.value = {
    view: view.value,
    masterId: detailMasterId.value,
    tab: request.returnTab || detailInitialTab.value,
    label: request.returnLabel || '返回上一页',
  }
  view.value = 'trace'
}
function returnFromTrace() {
  const target = traceReturn.value
  if (target?.view === 'master') {
    detailMasterId.value = target.masterId
    detailInitialTab.value = target.tab || 'runs'
    view.value = 'master'
  } else {
    view.value = target?.view || 'multi'
  }
  traceReturn.value = null
}

// 全局模态输入（替代 window.prompt）
const modal = ref(null)
let offPrompt = null
// 全局二次确认（替代 window.confirm，同样在内嵌浏览器不可用）
const confirmBox = ref(null)
let offConfirm = null
onMounted(() => {
  offPrompt = onModalPrompt(({ message, defaultValue, resolve }) => {
    modal.value = { message, value: defaultValue, resolve }
  })
  offConfirm = onModalConfirm(({ message, okText, danger, resolve }) => {
    confirmBox.value = { message, okText, danger, resolve }
  })
  if (authed.value) loadAccess(false)
  window.addEventListener('focus', refreshOnFocus)
  window.addEventListener('eagent-forbidden', refreshOnFocus)
})
onBeforeUnmount(() => {
  if (offPrompt) offPrompt()
  if (offConfirm) offConfirm()
  window.removeEventListener('focus', refreshOnFocus)
  window.removeEventListener('eagent-forbidden', refreshOnFocus)
})
async function loadAccess(afterLogin) {
  try {
    await refreshPageAccess()
    admin.value = pageAccess.isAdmin
    if (afterLogin) view.value = admin.value ? 'multi' : 'me'
    if (!canViewPage(view.value)) view.value = 'me'
  } catch {
    if (!admin.value) view.value = 'me'
  }
}
function refreshOnFocus() {
  if (authed.value) loadAccess(false)
}
const hasGovernanceNav = computed(() =>
  ['permission', 'loop', 'release', 'installation', 'trace', 'instance', 'enhance', 'simulator']
    .some(canViewPage))
function modalOk() {
  const m = modal.value
  modal.value = null
  m.resolve(m.value)
}
function modalCancel() {
  const m = modal.value
  modal.value = null
  m.resolve(null)
}
function confirmResolve(ok) {
  const c = confirmBox.value
  confirmBox.value = null
  c.resolve(ok)
}
</script>

<template>
  <!-- 登录门禁（admin-auth 01） -->
  <LoginView v-if="!authed" @logged-in="onLoggedIn" />
  <div v-else class="flex min-h-screen" :class="{ 'simulator-layout': view === 'simulator', 'simulator-nav-open': simulatorNavOpen }">
    <button v-if="view==='simulator'" class="simulator-mobile-nav" :aria-expanded="simulatorNavOpen" @click="simulatorNavOpen=!simulatorNavOpen">{{ simulatorNavOpen?'收起导航':'☰ 导航' }}</button>
    <!-- 侧边栏 -->
    <aside class="w-64 bg-white border-r border-line flex flex-col sticky top-0 h-screen shrink-0">
      <div class="h-16 flex items-center gap-2.5 px-5 border-b border-line">
        <div class="w-8 h-8 rounded-lg bg-brand flex items-center justify-center">
          <svg class="w-5 h-5 text-white" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M12 2l8 4v6c0 5-3.5 8.5-8 10-4.5-1.5-8-5-8-10V6l8-4z"/><path d="M9 12l2 2 4-4"/></svg>
        </div>
        <span class="text-[17px] font-semibold text-brand">eAgent 网关</span>
      </div>
      <nav class="flex-1 p-3 overflow-y-auto">
        <div v-if="canViewPage('multi')" :class="['flex items-center gap-3 h-11 px-3 rounded-lg cursor-pointer font-medium mb-0.5 border transition-colors',
          view==='multi' ? 'bg-brand-light text-brand border-brand' : 'border-transparent hover:bg-canvas']" @click="nav('multi')">
          <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87"/><path d="M16 3.13a4 4 0 010 7.75"/></svg>
          Agent Team
        </div>
        <div :class="['flex items-center gap-3 h-11 px-3 rounded-lg cursor-pointer font-medium mb-0.5 border transition-colors',
          view==='me' ? 'bg-brand-light text-brand border-brand' : 'border-transparent hover:bg-canvas']" @click="nav('me')">
          <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M20 21v-2a4 4 0 00-4-4H8a4 4 0 00-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
          我的权限
        </div>
        <div v-if="canViewPage('registry')" :class="['flex items-center gap-3 h-11 px-3 rounded-lg cursor-pointer font-medium mb-0.5 border transition-colors',
          view==='registry' ? 'bg-brand-light text-brand border-brand' : 'border-transparent hover:bg-canvas']" @click="nav('registry')">
          <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M4 7h16M4 12h16M4 17h16"/><circle cx="8" cy="7" r="1.6"/><circle cx="13" cy="12" r="1.6"/><circle cx="8" cy="17" r="1.6"/></svg>
          服务注册
        </div>
        <div v-if="canViewPage('kb')" :class="['flex items-center gap-3 h-11 px-3 rounded-lg cursor-pointer font-medium mb-0.5 border transition-colors',
          view==='kb' ? 'bg-brand-light text-brand border-brand' : 'border-transparent hover:bg-canvas']" @click="nav('kb')">
          <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M4 19.5A2.5 2.5 0 016.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 014 19.5v-15A2.5 2.5 0 016.5 2z"/><circle cx="12" cy="8" r="2"/><path d="M9.5 13.5h5"/></svg>
          知识库
        </div>
        <div v-if="canViewPage('performance')" :class="['flex items-center gap-3 h-11 px-3 rounded-lg cursor-pointer font-medium mb-0.5 border transition-colors',
          view==='performance' ? 'bg-brand-light text-brand border-brand' : 'border-transparent hover:bg-canvas']" @click="nav('performance')">
          <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M3 17l5-6 4 3 6-8"/><path d="M14 6h4v4"/><path d="M3 21h18"/></svg>
          性能测试
        </div>
        <div v-if="canViewPage('gateway')" :class="['flex items-center gap-3 h-11 px-3 rounded-lg cursor-pointer font-medium mb-0.5 border transition-colors',
          view==='gateway' ? 'bg-brand-light text-brand border-brand' : 'border-transparent hover:bg-canvas']" @click="nav('gateway')">
          <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M3 7h18M3 12h18M3 17h18"/><circle cx="7" cy="7" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="17" cy="17" r="1.6"/></svg>
          网关配置
        </div>
        <div v-if="hasGovernanceNav" class="mt-3.5 mx-3 mb-1.5 text-xs text-t3 font-medium">治理</div>
        <div v-if="canViewPage('permission')" :class="['flex items-center gap-3 h-11 px-3 rounded-lg cursor-pointer font-medium mb-0.5 border transition-colors',
          view==='permission' ? 'bg-brand-light text-brand border-brand' : 'border-transparent hover:bg-canvas']" @click="nav('permission')">
          <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M17 21v-2a4 4 0 00-4-4H6a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 00-3-3.87"/><path d="M16 3.13a4 4 0 010 7.75"/></svg>
          权限管理
        </div>
        <div v-if="canViewPage('loop')" :class="['flex items-center gap-3 h-11 px-3 rounded-lg cursor-pointer font-medium mb-0.5 border transition-colors',
          view==='loop' ? 'bg-brand-light text-brand border-brand' : 'border-transparent hover:bg-canvas']" @click="nav('loop')">
          <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M21 12a9 9 0 11-2.64-6.36L21 8"/><path d="M21 3v5h-5"/></svg>
          提示词调优
        </div>
        <div v-if="canViewPage('release')" :class="['flex items-center gap-3 h-11 px-3 rounded-lg cursor-pointer font-medium mb-0.5 border transition-colors',
          view==='release' ? 'bg-brand-light text-brand border-brand' : 'border-transparent hover:bg-canvas']" @click="nav('release')">
          <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M3 12a9 9 0 109-9 9 9 0 00-6.36 2.64L3 8"/><path d="M3 3v5h5"/></svg>
          版本发布
        </div>
        <div v-if="canViewPage('installation')" :class="['flex items-center gap-3 h-11 px-3 rounded-lg cursor-pointer font-medium mb-0.5 border transition-colors',
          view==='installation' ? 'bg-brand-light text-brand border-brand' : 'border-transparent hover:bg-canvas']" @click="nav('installation')">
          <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M12 3l8 4.5v9L12 21l-8-4.5v-9L12 3z"/><path d="M12 12l8-4.5M12 12v9M12 12L4 7.5"/></svg>
          工作空间安装
        </div>
        <div v-if="canViewPage('trace')" :class="['flex items-center gap-3 h-11 px-3 rounded-lg cursor-pointer font-medium mb-0.5 border transition-colors',
          view==='trace' ? 'bg-brand-light text-brand border-brand' : 'border-transparent hover:bg-canvas']" @click="nav('trace')">
          <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M3 6h4l4 12h4l4-12"/><circle cx="19" cy="6" r="1.8"/></svg>
          调用链路追踪
        </div>
        <div v-if="canViewPage('instance')" :class="['flex items-center gap-3 h-11 px-3 rounded-lg cursor-pointer font-medium mb-0.5 border transition-colors',
          view==='instance' ? 'bg-brand-light text-brand border-brand' : 'border-transparent hover:bg-canvas']" @click="nav('instance')">
          <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3" y="4" width="18" height="7" rx="1.5"/><rect x="3" y="13" width="18" height="7" rx="1.5"/><path d="M7 7.5h.01M7 16.5h.01"/></svg>
          服务实例
        </div>
        <div v-if="canViewPage('enhance')" :class="['flex items-center gap-3 h-11 px-3 rounded-lg cursor-pointer font-medium mb-0.5 border transition-colors',
          view==='enhance' ? 'bg-brand-light text-brand border-brand' : 'border-transparent hover:bg-canvas']" @click="nav('enhance')">
          <svg class="w-5 h-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M13 2L3 14h7l-1 8 11-13h-7l0-7z"/></svg>
          平台增强
        </div>
        <button v-if="canViewPage('simulator')" class="flex items-center gap-3 w-full h-11 px-3 rounded-lg text-left" :class="view==='simulator'?'bg-brand-light text-brand border border-brand':'hover:bg-canvas'" @click="nav('simulator')">Agent 模拟中心</button>
      </nav>
      <div class="p-4 border-t border-line flex items-center justify-between gap-2">
        <span class="text-xs text-t2 truncate" :title="sessionUser || api.user">{{ sessionUser || api.user || '未登录' }}</span>
        <button class="btn btn-text text-xs" @click="onLogout">退出</button>
      </div>
      <div class="p-4 border-t border-line text-xs text-t3 leading-relaxed">
        eAgent · v0.1.0<br>AgentScope 2.0.1 · A2A
      </div>
    </aside>

    <!-- 主区 -->
    <div class="flex-1 flex flex-col min-w-0">
      <header class="h-16 bg-white border-b border-line flex items-center justify-between px-6">
        <div class="flex items-center gap-2">
          <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M3 10.5L12 3l9 7.5"/><path d="M5 9.5V21h14V9.5"/></svg>
          <span class="text-t2">首页</span><span class="text-t3">/</span>
          <span class="font-medium">{{ crumb }}</span>
        </div>
        <div class="flex items-center gap-3">
          <div class="w-9 h-9 rounded-full bg-brand-light text-brand flex items-center justify-center font-semibold">e</div>
          <div>
            <div class="font-medium">{{ sessionUser || '未登录' }}</div>
            <span v-if="admin" class="text-xs text-white bg-brand px-1.5 py-px rounded">管理员</span>
          </div>
        </div>
      </header>

      <div class="flex-1 px-8 pt-7 pb-12">
        <TeamView v-if="view==='multi'" :can-manage="canManagePage('multi')" @open="openMaster" />
        <MyPermissionView v-else-if="view==='me'" />
        <SimulatorView v-else-if="view==='simulator' && admin" />
        <RegistryView v-else-if="view==='registry'" :can-manage="canManagePage('registry')" />
        <KbView v-else-if="view==='kb'" :can-manage="canManagePage('kb')" />
        <PerformanceView v-else-if="view==='performance'" :can-manage="canManagePage('performance')" />
        <GatewayView v-else-if="view==='gateway'" :can-manage="canManagePage('gateway')" />
        <MasterDetailView v-else-if="view==='master'" :master-id="detailMasterId"
          :initial-tab="detailInitialTab" :can-manage="canManagePage('multi')"
          :can-loop-manage="canManagePage('loop')"
          @back="nav('multi')" @open-trace="openTrace" />
        <PermissionView v-else-if="view==='permission'" />
        <LoopEngineerView v-else-if="view==='loop'" :can-manage="canManagePage('loop')" @open-trace="openTrace" />
        <ReleaseView v-else-if="view==='release'" :can-manage="canManagePage('release')" />
        <InstallationView v-else-if="view==='installation'" :can-manage="canManagePage('installation')" />
        <TraceView v-else-if="view==='trace'" :initial-trace-id="traceQueryId"
          :can-go-back="!!traceReturn" :return-label="traceReturn?.label || ''"
          @back="returnFromTrace" />
        <InstanceView v-else-if="view==='instance'" :can-manage="canManagePage('instance')" />
        <EnhancementView v-else-if="view==='enhance'" :can-manage="canManagePage('enhance')" />
      </div>
    </div>

    <!-- 全局通知（E1: 统一错误展示 code/message/traceId） -->
    <div class="fixed top-4 right-4 z-50 space-y-2 w-[380px]">
      <div v-for="t in toasts" :key="t.id"
           :class="['bg-white shadow-lg rounded-lg px-4 py-3 text-sm cursor-pointer border',
                    t.kind === 'success' ? 'border-up-light' : 'border-red-200']"
           @click="dismiss(t.id)">
        <div class="flex items-start gap-2">
          <svg v-if="t.kind === 'success'" class="w-4 h-4 text-up mt-0.5 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M8.5 12.5l2.5 2.5 4.5-5"/></svg>
          <svg v-else class="w-4 h-4 text-red-500 mt-0.5 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 8v4M12 16h.01"/></svg>
          <div class="min-w-0">
            <div v-if="t.kind !== 'success'" class="font-medium text-red-600">{{ t.code || 'REQUEST_FAILED' }}</div>
            <div :class="t.kind === 'success' ? 'text-t1' : 'text-t2 break-words'">{{ t.message }}</div>
            <div v-if="t.traceId" class="text-xs text-t3 mt-1 font-mono">traceId: {{ t.traceId }}</div>
          </div>
        </div>
      </div>
    </div>

    <!-- 全局模态输入 -->
    <div v-if="modal" class="fixed inset-0 bg-black/30 flex items-center justify-center z-50" @click.self="modalCancel">
      <div class="bg-white rounded-xl p-6 w-[440px] shadow-xl">
        <h3 class="font-semibold text-base mb-4">{{ modal.message }}</h3>
        <input v-model="modal.value" class="input" @keyup.enter="modalOk" autofocus />
        <div class="flex justify-end gap-2 mt-4">
          <button class="btn btn-ghost" @click="modalCancel">取消</button>
          <button class="btn btn-primary" @click="modalOk">确定</button>
        </div>
      </div>
    </div>

    <!-- 全局二次确认 -->
    <div v-if="confirmBox" class="fixed inset-0 bg-black/30 flex items-center justify-center z-50 p-4"
         @click.self="confirmResolve(false)">
      <div class="bg-white rounded-xl p-6 w-[460px] shadow-xl">
        <div class="flex gap-3">
          <svg :class="['w-5 h-5 shrink-0 mt-0.5', confirmBox.danger ? 'text-brand' : 'text-warn']"
               viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9L2.6 17a2 2 0 001.7 3h15.4a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z"/></svg>
          <p class="text-sm leading-6">{{ confirmBox.message }}</p>
        </div>
        <div class="flex justify-end gap-2 mt-5">
          <button class="btn btn-ghost" @click="confirmResolve(false)">取消</button>
          <button class="btn btn-primary" @click="confirmResolve(true)">{{ confirmBox.okText }}</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.simulator-mobile-nav { display:none }
@media(max-width:767px) {
  .simulator-layout { flex-direction:column }
  .simulator-mobile-nav { display:block; text-align:left; padding:12px 16px; background:white; border-bottom:1px solid #e5e7eb }
  .simulator-layout > aside { display:none; width:100%; position:static; height:auto }
  .simulator-nav-open > aside { display:flex; max-height:55vh }
  .simulator-layout > div > header { padding:0 16px }
  .simulator-layout > div > div { padding:16px }
}
</style>
