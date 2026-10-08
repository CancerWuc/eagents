<script setup>
// 我的权限（permission-control v1）：普通用户的权限自助视图。
// 展示角色、可见服务（含来源：直授/哪个组）与权限申请渠道；
// 服务可见性由管理员在「权限管理」页分配，deny-by-default。
import { onMounted, ref } from 'vue'
import { api } from '../api.js'
import Pagination from '../components/Pagination.vue'
import { useServerPagination } from '../pagination.js'

const err = ref('')
const loading = ref(true)
const info = ref(null)
const pageInfo = ref(null)
const services = ref([])
const servicesPager = useServerPagination(services, () => '/api/v1/me/permissions', {
  onResult: result => { info.value = result },
})

onMounted(async () => {
  try {
    const [, pages] = await Promise.all([
      servicesPager.load(), api.myPagePermissions(),
    ])
    pageInfo.value = pages
  } catch (e) {
    err.value = e.message || String(e)
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <div>
    <div class="mb-5">
      <h1 class="text-xl font-semibold">我的权限</h1>
      <p class="text-t2 mt-1">查看你的角色与可用的 A2A 服务；需要新服务权限请联系管理员开通。</p>
    </div>

    <div v-if="err" class="card !py-4 flex items-center gap-2 text-brand">
      <svg class="w-4 h-4 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16h.01"/></svg>
      {{ err }}（请确认后端已启动）
    </div>

    <div v-if="loading" class="card"><div class="h-4 w-40 rounded bg-canvas animate-pulse"></div></div>

    <template v-if="info">
      <!-- 身份与角色 -->
      <div class="card">
        <div class="flex items-center gap-3">
          <div class="w-10 h-10 rounded-full bg-brand-light text-brand flex items-center justify-center font-semibold text-sm">
            {{ (info.userId || '?').slice(0, 2) }}
          </div>
          <div class="min-w-0">
            <div class="font-semibold">{{ info.userId }}</div>
            <div class="flex items-center gap-2 mt-1">
              <span :class="info.isAdmin ? 'badge badge-brand' : 'badge badge-gray'">
                {{ info.isAdmin ? '管理员' : '普通用户' }}
              </span>
              <span v-for="r in info.roles" :key="r" class="badge badge-gray">{{ r }}</span>
            </div>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="font-semibold mb-3">可访问页面</div>
        <div v-if="pageInfo?.permissions?.length" class="grid grid-cols-2 gap-3">
          <div v-for="p in pageInfo.permissions" :key="p.pageKey" class="border border-line rounded-lg px-3 py-2">
            <div class="flex items-center justify-between">
              <span class="font-medium">{{ p.name || p.pageKey }}</span>
              <span :class="p.level === 'manage' ? 'badge badge-brand' : 'badge badge-gray'">{{ p.level === 'manage' ? '管理' : '查看' }}</span>
            </div>
            <div class="text-xs text-t3 mt-1" :title="(p.sources || []).map(s => s.name).join('、')">
              来源：{{ (p.sources || []).map(s => s.name).join('、') || '管理员角色' }}
            </div>
          </div>
        </div>
        <div v-else class="text-t3 text-sm py-4">尚未获得其他页面权限。</div>
      </div>

      <!-- 可用服务 -->
      <div class="card">
        <div class="flex items-center justify-between mb-3">
          <span class="font-semibold">可用的 A2A 服务</span>
          <span class="text-t3 text-sm">{{ servicesPager.total }} 个</span>
        </div>

        <div v-if="info.services.length" class="overflow-x-auto">
          <table class="tbl">
            <thead>
              <tr><th>服务</th><th>标识</th><th>来源</th></tr>
            </thead>
            <tbody>
              <tr v-for="s in info.services" :key="s.serviceId">
                <td class="font-medium">{{ s.displayName }}</td>
                <td class="mono text-[12px]">{{ s.slug }}</td>
                <td>
                  <span v-if="s.source === 'direct'" class="badge badge-ok">个人直授</span>
                  <span v-else class="badge badge-ok" :title="s.groupName">组：{{ s.groupName }}</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <div v-else class="py-10 flex flex-col items-center text-center">
          <div class="w-12 h-12 rounded-xl bg-canvas text-t3 flex items-center justify-center mb-3">
            <svg class="w-6 h-6" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="4" y="10" width="16" height="10" rx="2"/><path d="M8 10V7a4 4 0 018 0v3"/></svg>
          </div>
          <div class="text-t1 font-medium">还没有可用的服务</div>
          <p class="text-t3 text-[13px] mt-2 max-w-md leading-6">
            服务权限按工号分配（deny-by-default）：未授权的服务在 Agent Team
            的工具箱里不可见、也无法调用。需要开通请联系管理员。
          </p>
        </div>
      </div>

      <Pagination :pager="servicesPager" />
      <!-- 申请渠道 -->
      <div class="card">
        <div class="flex items-start gap-3">
          <svg class="w-5 h-5 text-warn shrink-0 mt-0.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9L2.6 17a2 2 0 001.7 3h15.4a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z"/></svg>
          <div class="min-w-0">
            <div class="font-semibold">申请更多服务权限</div>
            <p v-if="info.adminContact" class="text-[13px] text-t2 mt-1 break-words">{{ info.adminContact }}</p>
            <p v-else class="text-[13px] text-t2 mt-1 leading-6">
              请联系平台管理员，说明<b>工号</b>与<b>需要使用的服务</b>，由管理员在
              「权限管理 → 个人直授 / 用户组」中为你的工号授权，授权后实时生效。
            </p>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>
