<script setup>
import { ref, reactive, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { api } from '../api.js'
import { promptText, confirmAction } from '../ui.js'
import { getSession } from '../auth.js'
import { success } from '../notify.js'
import { fmtDateTime as fmtTime } from '../format.js'
import { activeUserCandidates } from '../permission-users.js'

import Pagination from '../components/Pagination.vue'
import { useServerPagination } from '../pagination.js'
import { createPagination } from '../pagination-state.js'

const err = ref('')
const groups = ref([])
const services = ref([])
const subTab = ref('groups')
// 用户 Tab：登录用户列表 + 角色/状态管理
const adminUsers = ref([])
const usersLoading = ref(false)
const usersErr = ref('')
const userOpBusy = ref('')
const session = getSession() || {}
const me = session.userId || ''
const expandedUser = ref('')      // 行展开：正在查看权限明细的工号
const userDetail = ref(null)      // 权限明细（组/直授）
const detailLoading = ref(false)
// 当前用户组编辑状态：{ gid, name, members: string[], serviceIds: Set }。
const editing = ref({})
const userGrant = ref({ user: '', serviceIds: new Set() })
const grantSaved = ref('')       // 服务端快照指纹，用于判断未保存修改
const grantLoading = ref(false)  // 正在回填该工号的现有授权
const grantLoadedFor = ref('')   // 已回填的工号
const savingGrant = ref(false)
const directs = ref([])          // 已有的全部个人直授
const localLoginEnabled = ref(null)
const localLoginBusy = ref(false)
const directoryQuery = ref('')
const directoryLoading = ref(false)
const directoryResults = ref([])
const directoryTotal = ref(0)
const directoryErr = ref('')
const importingUser = ref('')
const pageCatalog = ref([])
const pageEditor = ref(null)
const pagePermissionBusy = ref(false)

const adminUserRows = ref([])
const groupsPager = useServerPagination(groups, () => '/api/v1/groups')
const directsPager = useServerPagination(directs, () => '/api/v1/users/directs')
const usersPager = useServerPagination(adminUserRows, () => '/api/v1/admin/users', {
  onResult: () => { expandedUser.value = ''; userDetail.value = null },
})
let searchedName = ''
const directoryPager = createPagination(async ({ page, pageSize }) => {
  const response = await api.searchUserDirectory({ name: searchedName, pageNo: page, pageSize })
  return { ...response, items: response.items || [], total: response.total ?? 0, page, pageSize }
}, reactive({}), result => {
  directoryResults.value = result.items
  directoryTotal.value = result.total
})
onBeforeUnmount(directoryPager.invalidate)

async function load() {
  try {
    await groupsPager.load()
    services.value = await api.listServices("admin")
    await directsPager.load()
    adminUsers.value = await api.listAdminUsers()  // 完整成员候选池，不能随列表分页
    await usersPager.load()
    await loadLocalLogin()
    if (!pageCatalog.value.length) {
      pageCatalog.value = (await api.pagePermissionCatalog()).pages || []
    }
    err.value = ''
  } catch (e) { err.value = e.message }
}
onMounted(load)

// ---- 用户 Tab ----
async function loadUsers() {
  usersLoading.value = true
  usersErr.value = ''
  try {
    adminUsers.value = await api.listAdminUsers()
    await usersPager.load()
    await loadLocalLogin()
  } catch (e) {
    usersErr.value = e.message || String(e)
  } finally {
    usersLoading.value = false
  }
}
function serviceName(id) {
  const s = services.value.find(x => x.service_id === id)
  return s ? (s.display_name || s.slug || id) : id
}
function openUsersTab() {
  subTab.value = 'users'
  loadUsers()
}
async function loadLocalLogin() {
  try {
    const resp = await api.getLocalLogin()
    localLoginEnabled.value = !!resp.enabled
  } catch { /* 页面其它能力不依赖该开关 */ }
}
function syncLabel(v) {
  if (v === 'synced') return 'Pluto 登录'
  if (v === 'verified') return 'CAP 录入'
  return v || '—'
}
async function toggleLocalLogin() {
  if (localLoginEnabled.value === null) return
  const next = !localLoginEnabled.value
  const ok = await confirmAction(
    next ? '确认开启本地 admin 登录入口？登录页会重新显示用户名/密码表单。'
         : '确认关闭本地 admin 登录入口？登录页将只显示 Pluto 登录；请确认已有 Pluto 管理员账号。',
    { okText: next ? '开启' : '关闭', danger: !next },
  )
  if (!ok) return
  localLoginBusy.value = true
  try {
    const resp = await api.setLocalLogin(next)
    localLoginEnabled.value = !!resp.enabled
    success(next ? '本地 admin 登录入口已开启' : '本地 admin 登录入口已关闭')
  } finally {
    localLoginBusy.value = false
  }
}
async function searchDirectoryUsers() {
  const name = directoryQuery.value.trim()
  if (!name) return
  directoryLoading.value = true
  directoryErr.value = ''
  try {
    searchedName = name
    await directoryPager.load({ key: name, reset: true })
  } catch (e) {
    directoryErr.value = e.message || String(e)
  } finally {
    directoryLoading.value = false
  }
}
async function importDirectoryUser(u) {
  const ok = await confirmAction(
    `确认录入 ${u.userId} ${u.displayName || ''}？后端会再次校验该用户的当前状态。`,
    { okText: '录入' },
  )
  if (!ok) return
  importingUser.value = u.userId
  try {
    await api.createAdminUser(u.userId)
    success(`已录入用户 ${u.userId}`)
    await loadUsers()
    await load()
  } finally {
    importingUser.value = ''
  }
}
async function toggleAdminRole(u) {
  const grant = !u.isAdmin
  const ok = await confirmAction(
    grant ? `确认授予 ${u.userId} 管理员角色？管理员可进入全部治理页面并分配权限。`
          : `确认回收 ${u.userId} 的管理员角色？回收后立即失去治理页面访问权。`,
    { okText: grant ? '授予' : '回收', danger: !grant },
  )
  if (!ok) return
  userOpBusy.value = u.userId
  try {
    await api.setUserRole(u.userId, grant)
    success(grant ? `已授予 ${u.userId} 管理员角色` : `已回收 ${u.userId} 管理员角色`)
    await loadUsers()
  } finally {
    userOpBusy.value = ''
  }
}
async function toggleDetail(u) {
  if (expandedUser.value === u.userId) { expandedUser.value = ''; return }
  expandedUser.value = u.userId
  userDetail.value = null
  detailLoading.value = true
  try {
    const result = await api.userPermissionDetail(u.userId)
    if (expandedUser.value === u.userId) userDetail.value = result
  } finally {
    detailLoading.value = false
  }
}
/** 三 Tab 联动：从用户明细一键切到个人直授并载入该工号现有授权。 */
function goDirect(u) {
  subTab.value = 'user'
  const direct = (userDetail.value?.direct || []).map(d => ({ service_id: d.serviceId }))
  pickDirect({ cap_user_name: u.userId, services: direct })
}
async function toggleUserStatus(u) {
  const disable = u.status === 'active'
  const ok = await confirmAction(
    disable ? `确认禁用 ${u.userId}？禁用后立即踢下线且无法再登录。`
            : `确认启用 ${u.userId}？启用后可重新登录。`,
    { okText: disable ? '禁用' : '启用', danger: disable },
  )
  if (!ok) return
  userOpBusy.value = u.userId
  try {
    await api.setUserStatus(u.userId, disable ? 'disabled' : 'active')
    success(disable ? `已禁用 ${u.userId}` : `已启用 ${u.userId}`)
    await loadUsers()
  } finally {
    userOpBusy.value = ''
  }
}

function permissionMap(items) {
  return Object.fromEntries((items || []).map(p => [p.pageKey, p.level]))
}
async function editUserPages(u) {
  const detail = await api.userPermissionDetail(u.userId)
  pageEditor.value = {
    type: 'user', id: u.userId, name: `${u.displayName}（${u.userId}）`,
    levels: permissionMap(detail.pagePermissions?.direct),
    effective: detail.pagePermissions?.effective || [],
  }
}
async function editGroupPages(g) {
  const result = await api.getGroupPagePermissions(g.group_id)
  pageEditor.value = {
    type: 'group', id: g.group_id, name: g.name,
    levels: permissionMap(result.permissions), effective: [],
  }
}
function setPageLevel(pageKey, level) {
  if (!pageEditor.value) return
  if (level) pageEditor.value.levels[pageKey] = level
  else delete pageEditor.value.levels[pageKey]
}
async function savePagePermissions() {
  const e = pageEditor.value
  if (!e) return
  const permissions = Object.entries(e.levels).map(([pageKey, level]) => ({ pageKey, level }))
  pagePermissionBusy.value = true
  try {
    if (e.type === 'user') await api.putUserPagePermissions(e.id, permissions)
    else await api.putGroupPagePermissions(e.id, permissions)
    success(`已保存${e.type === 'user' ? '用户' : '用户组'}页面权限`)
    pageEditor.value = null
    if (expandedUser.value === e.id) {
      const u = adminUserRows.value.find(item => item.userId === e.id)
      if (u) await toggleDetail(u).then(() => toggleDetail(u))
    }
  } finally {
    pagePermissionBusy.value = false
  }
}

const savingGroup = ref(false)
const deletingGroup = ref('')

async function createGroup() {
  const name = await promptText('用户组名称')
  if (!name || !name.trim()) return
  try {
    await api.createGroup(name.trim())
    await load()
    success(`用户组「${name.trim()}」已创建`)
  } catch { /* toast 已展示 */ }
}

async function removeGroup(g) {
  const ok = await confirmAction(
    `确认删除用户组「${g.name}」？会清理该组成员、服务可见性和页面权限，不会删除用户、服务或历史记录。`,
    { okText: '删除', danger: true },
  )
  if (!ok) return
  deletingGroup.value = g.group_id
  try {
    await api.deleteGroup(g.group_id)
    if (editing.value.gid === g.group_id) editing.value = {}
    await load()
    success(`用户组「${g.name}」已删除`)
  } finally {
    deletingGroup.value = ''
  }
}

function edit(g) {
  editing.value = {
    gid: g.group_id,
    members: [...g.members],          // 数组而非逗号字符串：每个工号是独立可删的标签
    serviceIds: new Set(g.service_ids),
    name: g.name,
  }
  memberQuery.value = ''
}

// --- 成员选择器 -----------------------------------------------------------
const memberQuery = ref('')
const USER_RE = /^[A-Za-z0-9._@-]{1,64}$/

/** 候选 = 候选池 - 已选，按输入过滤。 */
const memberCandidates = computed(() => activeUserCandidates(adminUsers.value, memberQuery.value, {
  exclude: editing.value.members || [], limit: 8,
}))

const memberQueryError = computed(() => {
  const v = memberQuery.value.trim()
  if (!v) return ''
  if (!USER_RE.test(v)) return '工号只能是字母、数字与 . _ @ -（最多 64 位）'
  if ((editing.value.members || []).includes(v)) return '该工号已在本组'
  return ''
})
/** 精确命中启用用户库中的候选后，才允许按输入添加。 */
const canAddTyped = computed(() => {
  const v = memberQuery.value.trim().toLowerCase()
  if (!v || memberQueryError.value) return false
  // 用户组成员必须来自已录入或已登录建档且启用的用户库。
  return memberCandidates.value.some(u => u.cap_user_name.toLowerCase() === v)
})

function addMember(user) {
  const v = (user || '').trim()
  if (!v || !USER_RE.test(v)) return
  const activeUser = adminUsers.value.some(u => u.userId === v && u.status === 'active')
  if (!activeUser) return
  if (!editing.value.members.includes(v)) editing.value.members.push(v)
  memberQuery.value = ''
}
function removeMember(user) {
  editing.value.members = editing.value.members.filter(m => m !== user)
}
/** 支持粘贴一串工号（逗号/空格/换行分隔）批量加入。 */
function onMemberPaste(evt) {
  const text = (evt.clipboardData || window.clipboardData)?.getData('text') || ''
  const parts = text.split(/[,，;；\s]+/).filter(Boolean)
  if (parts.length < 2) return          // 单个值走正常输入流程
  evt.preventDefault()
  for (const p of parts) addMember(p)
}
const isKnown = (user) =>
  adminUsers.value.some(u => u.userId === user && u.status === 'active')

async function saveEdit() {
  const e = editing.value
  const members = [...new Set(e.members)]
  savingGroup.value = true
  try {
    // 两次写入无事务：成员写成功而服务写失败会留下半保存状态，因此失败时保留
    // 弹窗不清空，让用户看到并重试
    await api.putGroupMembers(e.gid, members)
    await api.putGroupServices(e.gid, [...e.serviceIds])
    editing.value = {}
    await load()
    success(`用户组「${e.name}」已保存：${members.length} 名成员，${[...e.serviceIds].length} 个可见服务`)
  } catch { /* toast 已展示，弹窗保留供重试 */ } finally {
    savingGroup.value = false
  }
}

function toggleSet(set, id) { set.has(id) ? set.delete(id) : set.add(id) }

// --- 个人直授 -------------------------------------------------------------
const grantUserId = computed(() => userGrant.value.user.trim())
const grantFingerprint = () => JSON.stringify([...userGrant.value.serviceIds].sort())
const grantDirty = computed(() => grantFingerprint() !== grantSaved.value)
// 工号为空时 URL 会拼成 /api/v1/users//services，后端匹配不到路由回一个与业务
// 无关的 404。这里前置拦住，不让它变成看不懂的报错。
const grantError = computed(() => {
  const v = grantUserId.value
  if (!v) return '请先填写工号'
  if (!USER_RE.test(v)) return '工号只能是字母、数字与 . _ @ -（最多 64 位）'
  const user = adminUsers.value.find(u => u.userId === v)
  if (!user) return '该工号不在用户表中，请先录入或让员工登录建档'
  if (user.status !== 'active') return '该用户已禁用，不能分配个人直授'
  return ''
})
const grantReady = computed(() =>
  !grantError.value && !grantLoading.value && grantDirty.value)

/** 工号候选：已精确命中时不再提示，避免选完下拉框还挂着。 */
const grantCandidates = computed(() => {
  const q = grantUserId.value.toLowerCase()
  if (!q) return []
  if (adminUsers.value.some(u => u.userId === grantUserId.value)) return []
  return activeUserCandidates(adminUsers.value, q, { limit: 6 })
})

/** PUT 是全量替换，进入前必须回填该工号现有授权，否则保存会静默清空旧记录。 */
async function loadUserGrant(user) {
  if (!user || grantError.value) {
    userGrant.value.serviceIds = new Set()
    grantSaved.value = JSON.stringify([])
    grantLoadedFor.value = ''
    return
  }
  grantLoading.value = true
  try {
    const resp = await api.getUserServices(user)
    userGrant.value.serviceIds = new Set(resp.service_ids || [])
    grantSaved.value = grantFingerprint()
    grantLoadedFor.value = user
  } catch {
    grantLoadedFor.value = ''
  } finally {
    grantLoading.value = false
  }
}

// 工号改动后防抖回填
let grantTimer = null
watch(grantUserId, (v) => {
  clearTimeout(grantTimer)
  grantTimer = setTimeout(() => loadUserGrant(v), 400)
})

async function grantUser() {
  if (!grantReady.value) return
  savingGrant.value = true
  try {
    const ids = [...userGrant.value.serviceIds]
    await api.putUserServices(grantUserId.value, ids)
    grantSaved.value = grantFingerprint()
    await directsPager.load()
    success(`已保存 ${grantUserId.value} 的个人直授：${ids.length} 个服务`)
  } catch { /* toast 已展示 */ } finally {
    savingGrant.value = false
  }
}

/** 点已有记录直接载入编辑。 */
function pickDirect(u) {
  userGrant.value.user = u.cap_user_name
  // watch 的防抖回填会接着跑，这里先按已知数据即时填上，避免闪烁
  userGrant.value.serviceIds = new Set(u.services.map(s => s.service_id))
  grantSaved.value = grantFingerprint()
  grantLoadedFor.value = u.cap_user_name
}
</script>

<template>
  <div>
    <div class="flex items-start justify-between mb-5">
      <div>
        <h1 class="text-xl font-semibold">权限管理</h1>
        <p class="text-t2 mt-1">按工号（cap-user-name）/ 用户组，控制用户可见的 A2A 服务子集</p>
      </div>
      <button class="btn btn-primary" @click="createGroup">
        <svg class="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M12 5v14M5 12h14"/></svg>新建用户组
      </button>
    </div>

    <div v-if="err" class="card text-brand">{{ err }}</div>

    <div class="flex gap-2 mb-5">
      <button :class="['h-9 px-4 rounded-md text-sm font-medium border',
        subTab==='groups' ? 'bg-brand text-white border-brand' : 'bg-white border-line']" @click="subTab='groups'">用户组</button>
      <button :class="['h-9 px-4 rounded-md text-sm font-medium border',
        subTab==='user' ? 'bg-brand text-white border-brand' : 'bg-white border-line']" @click="subTab='user'">个人直授</button>
      <button :class="['h-9 px-4 rounded-md text-sm font-medium border',
        subTab==='users' ? 'bg-brand text-white border-brand' : 'bg-white border-line']" @click="openUsersTab">用户</button>
    </div>

    <div v-if="subTab==='groups'" class="card !p-0 overflow-hidden">
      <!-- 服务明细收进编辑弹窗，列表只做汇总：服务再多也不需要横向滚动 -->
      <table class="tbl">
        <thead><tr>
          <th class="!pl-6">用户组</th>
          <th class="!text-left">成员</th>
          <th class="!text-left">可见服务</th>
          <th class="w-36 !pr-6"></th>
        </tr></thead>
        <tbody>
          <tr v-for="g in groups" :key="g.group_id">
            <td class="!pl-6 font-semibold whitespace-nowrap">{{ g.name }}</td>
            <td class="!text-left">
              <template v-if="g.members.length">
                <span v-for="m in g.members.slice(0, 3)" :key="m"
                      class="mono text-[12px] bg-canvas text-t2 px-1.5 py-0.5 rounded mr-1 inline-block">{{ m }}</span>
                <span v-if="g.members.length > 3" class="text-xs text-t3"
                      :title="g.members.join(', ')">+{{ g.members.length - 3 }}</span>
              </template>
              <span v-else class="text-t3 text-[13px]">暂无成员</span>
            </td>
            <td class="!text-left">
              <template v-if="g.service_ids.length">
                <span class="font-semibold">{{ g.service_ids.length }}</span>
                <span class="text-t3 text-[13px]"> 个</span>
                <span class="text-xs text-t3 ml-1.5"
                      :title="g.service_ids.map(id => serviceName(id)).join('、')">
                  {{ g.service_ids.slice(0, 2).map(id => serviceName(id)).join('、') }}{{ g.service_ids.length > 2 ? ' …' : '' }}
                </span>
              </template>
              <span v-else class="text-t3 text-[13px]">未授权服务</span>
            </td>
            <td class="!pr-6 whitespace-nowrap">
              <button class="btn btn-text" @click="edit(g)">编辑</button>
              <button class="btn btn-text" @click="editGroupPages(g)">页面权限</button>
              <button class="btn btn-text text-red-600" :disabled="deletingGroup === g.group_id" @click="removeGroup(g)">
                {{ deletingGroup === g.group_id ? '删除中…' : '删除' }}
              </button>
            </td>
          </tr>
          <tr v-if="!groups.length"><td colspan="4" class="!py-10 text-center text-t3">暂无用户组</td></tr>
        </tbody>
      </table>
      <Pagination :pager="groupsPager" />
    </div>

    <div v-if="subTab==='groups'" class="card">
      <h3 class="font-semibold mb-1">说明</h3>
      <p class="text-t2 text-[13px] leading-7">运行时身份取自请求头 <code class="mono">cap-user-name</code>（工号）。
        可见集合 = 组可见 ∪ 个人直授；Agent Team 的 Toolkit 按该集合裁剪——用户不可见的服务，模型既看不到也无法调用。</p>
    </div>

    <template v-if="subTab==='user'">
    <div class="card">
      <div class="flex items-start justify-between mb-4">
        <div>
          <h3 class="font-semibold">个人直授</h3>
          <p class="text-t2 text-[13px] mt-1">
            保存为<strong>全量替换</strong>：提交的勾选集合即该工号最终的直授集合，未勾上的会被移除。
          </p>
        </div>
        <span v-if="grantDirty && !grantError" class="badge badge-warn shrink-0">有未保存修改</span>
      </div>

      <div class="mb-4 max-w-sm">
        <label class="block text-[13px] text-t2 mb-1.5">工号（cap-user-name）<span class="text-brand">*</span></label>
        <div class="relative">
          <input v-model="userGrant.user" class="input" :class="grantError && userGrant.user && '!border-brand'"
                 placeholder="user-id" @keydown.enter="grantUser" />
          <svg v-if="grantLoading" class="w-4 h-4 animate-spin text-t3 absolute right-3 top-2.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M21 12a9 9 0 11-2.64-6.36"/></svg>
          <!-- 候选提示：个人直授只从登录用户库选择 -->
          <div v-if="grantCandidates.length"
               class="mt-1.5 border border-line rounded-lg bg-white divide-y divide-line overflow-hidden">
            <button v-for="u in grantCandidates" :key="u.cap_user_name"
                    class="w-full flex items-center justify-between px-3 py-2 hover:bg-canvas text-left"
                    @click="userGrant.user = u.cap_user_name">
              <span class="mono text-[13px]">{{ u.cap_user_name }}</span>
              <span class="text-xs text-t3 shrink-0 ml-2">
                {{ [u.display_name, u.department].filter(Boolean).join(' · ') }}
              </span>
            </button>
          </div>
        </div>
        <p :class="['text-xs mt-1.5', grantError && userGrant.user ? 'text-brand' : 'text-t3']">
          <template v-if="grantError">{{ grantError }}</template>
          <template v-else-if="grantLoading">正在读取该工号现有直授…</template>
          <template v-else-if="grantLoadedFor === grantUserId">已载入现有直授，可直接增减后保存</template>
          <template v-else>个人直授只能分配给已录入或已登录建档的启用用户</template>
        </p>
      </div>

      <div v-if="services.length" class="flex flex-wrap gap-4 mb-4">
        <label v-for="s in services" :key="s.service_id"
               :class="['flex items-center gap-2 text-sm', grantError ? 'opacity-50' : 'cursor-pointer']">
          <input type="checkbox" class="accent-[#C22128] w-4 h-4" :disabled="!!grantError || grantLoading"
            :checked="userGrant.serviceIds.has(s.service_id)"
            @change="toggleSet(userGrant.serviceIds, s.service_id)" />
          {{ s.display_name }}（标识：{{ s.slug }}）
        </label>
      </div>
      <p v-else class="text-t3 text-[13px] mb-4">还没有已注册的 A2A 服务，先去「A2A 注册」注册服务。</p>

      <div class="flex items-center gap-3">
        <button class="btn btn-primary" :disabled="!grantReady || savingGrant" @click="grantUser">
          <svg v-if="savingGrant" class="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M21 12a9 9 0 11-2.64-6.36"/></svg>
          {{ savingGrant ? '保存中…' : '保存直授' }}
        </button>
        <span v-if="grantError" class="text-[13px] text-t3">{{ grantError }}后可保存</span>
        <span v-else-if="!grantDirty" class="text-[13px] text-t3">当前勾选与已保存内容一致</span>
      </div>
    </div>

    <!-- 已有直授总览 -->
    <div class="card !p-0">
      <div class="flex items-center justify-between px-6 py-4 border-b border-line">
        <h3 class="font-semibold">已有个人直授
          <span class="text-t2 text-[13px] font-normal">（{{ directsPager.total }} 个工号，点击载入编辑）</span>
        </h3>
      </div>
      <table class="tbl">
        <thead><tr><th class="!pl-6">工号</th><th>直授服务</th><th class="w-24 !pr-6"></th></tr></thead>
        <tbody>
          <tr v-for="u in directs" :key="u.cap_user_name">
            <td class="!pl-6 mono">{{ u.cap_user_name }}</td>
            <td>
              <span v-for="s in u.services" :key="s.service_id" class="badge badge-gray mr-1.5">
                {{ s.display_name || s.slug || s.service_id }}
              </span>
            </td>
            <td class="!pr-6"><button class="btn btn-text" @click="pickDirect(u)">载入</button></td>
          </tr>
          <tr v-if="!directs.length">
            <td colspan="3" class="!py-10 text-center text-t3">
              暂无个人直授记录 —— 上方选择已录入或已登录建档用户并勾选服务即可创建
            </td>
          </tr>
        </tbody>
      </table>
      <Pagination :pager="directsPager" />
    </div>
    </template>

    <!-- 用户 Tab：用户库 + CAP 录入 + 角色/状态管理 -->
    <template v-if="subTab==='users'">
    <div class="card mb-4">
      <div class="flex items-start justify-between gap-4">
        <div>
          <h3 class="font-semibold">本地 admin 登录入口</h3>
          <p class="text-t2 text-[13px] mt-1">
            初始化和兜底使用。关闭后登录页不再展示用户名/密码表单，只保留 Pluto 登录。
          </p>
        </div>
        <button :class="['btn', localLoginEnabled ? 'btn-ghost text-red-600' : 'btn-primary']"
                :disabled="localLoginEnabled === null || localLoginBusy"
                @click="toggleLocalLogin">
          {{ localLoginBusy ? '处理中…' : (localLoginEnabled ? '关闭本地登录' : '开启本地登录') }}
        </button>
      </div>
      <p class="text-xs text-t3 mt-2">当前状态：{{ localLoginEnabled === null ? '读取中' : (localLoginEnabled ? '已开启' : '已关闭，前端登录页不可见') }}</p>
    </div>

    <div class="card mb-4">
      <div class="mb-4">
        <div>
          <h3 class="font-semibold">录入真实用户</h3>
          <p class="text-t2 text-[13px] mt-1">
            按姓名或工号查询用户目录；只能从查询结果录入，保存前后端会再次校验。
          </p>
        </div>
      </div>
      <div class="flex gap-2 max-w-xl">
        <input v-model="directoryQuery" class="input"
               placeholder="输入姓名或工号搜索 CAP 用户" @keydown.enter="searchDirectoryUsers" />
        <button class="btn btn-primary shrink-0" :disabled="!directoryQuery.trim() || directoryLoading"
                @click="searchDirectoryUsers">
          {{ directoryLoading ? '搜索中…' : '搜索' }}
        </button>
      </div>
      <p v-if="directoryErr" class="text-xs text-brand mt-2">{{ directoryErr }}</p>
      <div v-if="directoryResults.length" class="mt-4 border border-line rounded-lg overflow-hidden">
        <table class="tbl">
          <thead><tr><th class="!pl-4">工号</th><th>姓名 / 部门</th><th>邮箱</th><th>状态</th><th class="!pr-4 w-24"></th></tr></thead>
          <tbody>
            <tr v-for="u in directoryResults" :key="u.userId">
              <td class="!pl-4 mono whitespace-nowrap">{{ u.userId }}</td>
              <td class="whitespace-nowrap">
                <span class="font-medium">{{ u.displayName || u.userId }}</span>
                <span v-if="u.department" class="text-xs text-t3 ml-1.5">{{ u.department }}</span>
              </td>
              <td class="text-xs text-t3">{{ u.email || '—' }}</td>
              <td>
                <span :class="u.status === 'active' ? 'badge badge-ok' : 'badge badge-err'">
                  {{ u.status === 'active' ? '正常' : '已停用' }}
                </span>
              </td>
              <td class="!pr-4">
                <button class="btn btn-text" :disabled="u.status !== 'active' || importingUser === u.userId"
                        @click="importDirectoryUser(u)">
                  {{ importingUser === u.userId ? '录入中…' : '录入' }}
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-else-if="directoryQuery && !directoryLoading && !directoryErr" class="text-xs text-t3 mt-3">
        暂无查询结果
      </p>
      <Pagination v-if="searchedName || directoryPager.error" :pager="directoryPager" />

    </div>

    <div class="card !p-0 overflow-hidden">
      <div class="flex items-center justify-between px-6 py-4 border-b border-line">
        <div>
          <h3 class="font-semibold">用户库</h3>
          <p class="text-t2 text-[13px] mt-1">
            Pluto 登录或管理员从 CAP 录入的真实用户。授予管理员后可进入全部治理页面；服务可见性在「用户组 / 个人直授」中分配。
          </p>
        </div>
        <button class="btn btn-ghost btn-sm" :disabled="usersLoading" @click="loadUsers">
          <svg class="w-4 h-4" :class="usersLoading && 'animate-spin'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M21 12a9 9 0 11-2.64-6.36L21 8"/><path d="M21 3v5h-5"/></svg>
          刷新
        </button>
      </div>
      <div v-if="usersErr" class="px-6 py-3 text-brand text-sm">{{ usersErr }}</div>
      <div class="overflow-x-auto">
        <table class="tbl">
          <thead><tr>
            <th class="!pl-6">工号</th><th>姓名 / 部门</th><th>来源</th><th>角色</th>
            <th>可见服务</th><th>最后登录</th><th>状态</th><th class="!pr-6">操作</th>
          </tr></thead>
          <tbody>
            <template v-for="u in adminUserRows" :key="u.userId">
            <tr>
              <td class="!pl-6 mono whitespace-nowrap">{{ u.userId }}</td>
              <td class="whitespace-nowrap">
                <span class="font-medium">{{ u.displayName }}</span>
                <span v-if="u.department" class="text-xs text-t3 ml-1.5">{{ u.department }}</span>
              </td>
              <td>
                <span class="badge badge-gray">{{ syncLabel(u.profileSyncStatus) }}</span>
              </td>
              <td>
                <span :class="u.isAdmin ? 'badge badge-brand' : 'badge badge-gray'">
                  {{ u.isAdmin ? '管理员' : '普通用户' }}
                </span>
              </td>
              <td class="whitespace-nowrap">
                {{ u.visibleServices }} 个
                <button class="btn btn-text !h-6 text-[12px] ml-1"
                        title="查看权限明细（组/直授来源）"
                        @click="toggleDetail(u)">
                  {{ expandedUser === u.userId ? '收起' : '明细' }}
                </button>
              </td>
              <td class="text-xs text-t3 whitespace-nowrap">{{ fmtTime(u.lastLoginAt) }}</td>
              <td>
                <span :class="u.status === 'active' ? 'badge badge-ok' : 'badge badge-err'">
                  {{ u.status === 'active' ? '正常' : '已禁用' }}
                </span>
              </td>
              <td class="!pr-6 whitespace-nowrap">
                <button class="btn btn-text" @click="editUserPages(u)">页面权限</button>
                <!-- 自保护：不能操作自己的角色/状态 -->
                <template v-if="u.userId !== me">
                  <button class="btn btn-text" :disabled="userOpBusy === u.userId"
                          :title="u.isAdmin ? '回收管理员角色' : '授予管理员角色'"
                          @click="toggleAdminRole(u)">
                    {{ u.isAdmin ? '回收管理' : '设为管理' }}
                  </button>
                  <button :class="['btn btn-text', u.status === 'active' ? 'text-red-600' : 'text-green-700']"
                          :disabled="userOpBusy === u.userId"
                          :title="u.status === 'active' ? '禁用后立即踢下线且无法登录' : '恢复登录'"
                          @click="toggleUserStatus(u)">
                    {{ u.status === 'active' ? '禁用' : '启用' }}
                  </button>
                </template>
                <span v-else class="text-xs text-t3" title="不能操作自己的账号">当前登录</span>
              </td>
            </tr>
            <!-- 行展开：权限明细（组/直授）+ 去直授联动 -->
            <tr v-if="expandedUser === u.userId">
              <td colspan="8" class="!py-4 bg-[#FAFBFC]">
                <div v-if="detailLoading" class="text-t3 text-[13px] px-2">加载明细中…</div>
                <template v-else-if="userDetail">
                  <div class="grid grid-cols-2 gap-4">
                    <div>
                      <div class="text-[13px] font-medium mb-1.5">所属组可见</div>
                      <div v-if="userDetail.groups.length" class="flex flex-col gap-1.5">
                        <div v-for="g in userDetail.groups" :key="g.groupId" class="text-[13px]">
                          <span class="font-medium">{{ g.groupName }}</span>
                          <span class="text-t3 text-xs ml-1">{{ g.memberCount }} 人</span>
                          <div class="flex flex-wrap gap-1 mt-1">
                            <span v-for="s in g.services" :key="s.serviceId" class="badge badge-ok">{{ s.displayName }}</span>
                          </div>
                        </div>
                      </div>
                      <div v-else class="text-t3 text-[13px]">未加入任何组</div>
                    </div>
                    <div>
                      <div class="text-[13px] font-medium mb-1.5 flex items-center justify-between">
                        <span>个人直授（{{ userDetail.direct.length }} 个）</span>
                        <button class="btn btn-text !h-6 text-[12px]"
                                title="切换到个人直授并载入该工号"
                                @click="goDirect(u)">去直授 →</button>
                      </div>
                      <div v-if="userDetail.direct.length" class="flex flex-wrap gap-1">
                        <span v-for="s in userDetail.direct" :key="s.serviceId" class="badge badge-ok">{{ s.displayName }}</span>
                      </div>
                      <div v-else class="text-t3 text-[13px]">无个人直授</div>
                    </div>
                  </div>
                </template>
              </td>
            </tr>
            </template>
            <tr v-if="!adminUserRows.length && !usersLoading">
              <td colspan="8" class="!py-10 text-center text-t3">
                还没有用户 —— 可从 CAP 录入，或员工经 Pluto 登录后自动建档
              </td>
            </tr>
          </tbody>
        </table>
      <Pagination :pager="usersPager" />
      </div>
    </div>
    </template>

    <!-- 编辑弹窗 -->
    <div v-if="editing.gid" class="fixed inset-0 bg-black/30 flex items-center justify-center z-50" @click.self="editing={}">
      <div class="bg-white rounded-xl p-6 w-[560px] shadow-xl max-h-[90vh] overflow-y-auto">
        <h3 class="font-semibold text-base mb-4">编辑用户组 · {{ editing.name }}</h3>
        <div class="mb-4">
          <label class="block text-[13px] text-t2 mb-1.5">
            成员 <span class="text-t3 font-normal">（{{ editing.members.length }} 人）</span>
          </label>

          <!-- 已选工号：每个是独立标签，点 × 移除 -->
          <div v-if="editing.members.length" class="flex flex-wrap gap-1.5 mb-2">
            <span v-for="m in editing.members" :key="m"
                  :class="['inline-flex items-center gap-1 text-[13px] px-2 py-1 rounded',
                           isKnown(m) ? 'bg-up-light text-up' : 'bg-canvas text-t2']"
                  :title="isKnown(m) ? '已建档启用用户' : '不在用户表或已禁用，保存时会被拒绝'">
              <span class="mono">{{ m }}</span>
              <button class="hover:text-brand" @click="removeMember(m)">
                <svg class="w-3 h-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M18 6L6 18M6 6l12 12"/></svg>
              </button>
            </span>
          </div>

          <!-- 搜索 / 输入 -->
          <div class="relative">
            <div class="flex items-center gap-2 border border-line rounded-lg px-3 bg-white focus-within:border-brand transition-colors"
                 :class="memberQueryError && '!border-brand'">
              <svg class="w-4 h-4 text-t3 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>
              <input v-model="memberQuery" class="h-9 flex-1 outline-none text-sm bg-transparent"
                     placeholder="搜索已建档用户，回车添加；也可粘贴一串批量添加"
                     @keydown.enter.prevent="canAddTyped && addMember(memberQuery)"
                     @paste="onMemberPaste" />
              <button v-if="canAddTyped" class="btn btn-text !h-7 shrink-0" @click="addMember(memberQuery)">添加</button>
            </div>

            <!-- 候选下拉 -->
            <div v-if="memberCandidates.length"
                 class="mt-1.5 border border-line rounded-lg bg-white divide-y divide-line overflow-hidden">
              <button v-for="u in memberCandidates" :key="u.cap_user_name"
                      class="w-full flex items-center justify-between px-3 py-2 hover:bg-canvas text-left"
                      @click="addMember(u.cap_user_name)">
                <span class="min-w-0">
                  <span class="mono text-[13px]">{{ u.cap_user_name }}</span>
                  <span v-if="u.display_name && u.display_name !== u.cap_user_name" class="text-xs text-t2 ml-1.5">{{ u.display_name }}</span>
                  <span v-if="u.department" class="text-xs text-t3 ml-1.5">{{ u.department }}</span>
                </span>
                <span class="text-xs shrink-0 ml-2 text-up">
                  用户库
                </span>
              </button>
            </div>
          </div>

          <p :class="['text-xs mt-1.5 leading-5', memberQueryError ? 'text-brand' : 'text-t3']">
            <template v-if="memberQueryError">{{ memberQueryError }}</template>
            <template v-else>
              用户组成员只能选择已录入或已登录建档且启用的用户。
            </template>
          </p>
        </div>
        <div class="mb-5">
          <label class="block text-[13px] text-t2 mb-1.5">可见服务</label>
          <div class="flex flex-col gap-2">
            <label v-for="s in services" :key="s.service_id" class="flex items-center gap-2 text-sm">
              <input type="checkbox" class="accent-[#C22128] w-4 h-4"
                :checked="editing.serviceIds.has(s.service_id)"
                @change="toggleSet(editing.serviceIds, s.service_id)" />
              {{ s.display_name }}（标识：{{ s.slug }}）
            </label>
          </div>
        </div>
        <div class="flex justify-end gap-2">
          <button class="btn btn-ghost" :disabled="savingGroup" @click="editing={}">取消</button>
          <button class="btn btn-primary" :disabled="savingGroup" @click="saveEdit">
            <svg v-if="savingGroup" class="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M21 12a9 9 0 11-2.64-6.36"/></svg>
            {{ savingGroup ? '保存中…' : '保存' }}
          </button>
        </div>
      </div>
    </div>

    <div v-if="pageEditor" class="fixed inset-0 bg-black/30 flex items-center justify-center z-50 p-4" @click.self="pageEditor=null">
      <div class="bg-white rounded-xl p-6 w-[680px] shadow-xl max-h-[90vh] overflow-y-auto">
        <h3 class="font-semibold text-base">页面权限 · {{ pageEditor.name }}</h3>
        <p class="text-xs text-t3 mt-1 mb-4">管理包含查看；未选择表示无权限。个人授权与所在用户组授权取最高级别。</p>
        <div class="border border-line rounded-lg divide-y divide-line">
          <div v-for="p in pageCatalog" :key="p.pageKey" class="grid grid-cols-[1fr_120px_120px_120px] items-center gap-2 px-4 py-3">
            <div>
              <div class="font-medium text-sm">{{ p.name }}</div>
              <div class="text-xs text-t3 mono">{{ p.pageKey }}</div>
            </div>
            <label class="text-sm"><input type="radio" :name="`page-${p.pageKey}`" :checked="!pageEditor.levels[p.pageKey]" @change="setPageLevel(p.pageKey, '')" /> 无权限</label>
            <label class="text-sm"><input type="radio" :name="`page-${p.pageKey}`" :checked="pageEditor.levels[p.pageKey]==='view'" @change="setPageLevel(p.pageKey, 'view')" /> 查看</label>
            <label class="text-sm" :class="!p.levels.includes('manage') && 'text-t3'">
              <input type="radio" :name="`page-${p.pageKey}`" :disabled="!p.levels.includes('manage')" :checked="pageEditor.levels[p.pageKey]==='manage'" @change="setPageLevel(p.pageKey, 'manage')" /> 管理
            </label>
          </div>
        </div>
        <div v-if="pageEditor.type==='user' && pageEditor.effective.length" class="mt-4">
          <div class="text-sm font-medium mb-2">当前有效权限与来源</div>
          <div class="flex flex-wrap gap-2">
            <span v-for="p in pageEditor.effective" :key="p.pageKey" class="badge badge-gray" :title="(p.sources || []).map(s => `${s.name}:${s.level || p.level}`).join('、')">
              {{ p.pageKey }} · {{ p.level === 'manage' ? '管理' : '查看' }}
            </span>
          </div>
        </div>
        <div class="flex justify-end gap-2 mt-5">
          <button class="btn btn-ghost" :disabled="pagePermissionBusy" @click="pageEditor=null">取消</button>
          <button class="btn btn-primary" :disabled="pagePermissionBusy" @click="savePagePermissions">{{ pagePermissionBusy ? '保存中…' : '保存' }}</button>
        </div>
      </div>
    </div>
  </div>
</template>
