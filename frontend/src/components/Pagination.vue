<script setup>
import { computed } from 'vue'
import { visiblePages } from '../pagination-state.js'

const props = defineProps({ pager: { type: Object, required: true } })
const totalPages = computed(() => Math.max(1, Math.ceil(props.pager.total / props.pager.pageSize)))
const pages = computed(() => visiblePages(props.pager.page, totalPages.value))
function load(options) { props.pager.load(options).catch(() => {}) }
</script>

<template>
  <div class="px-4 py-3 border-t border-line text-sm" aria-label="列表分页">
    <div v-if="pager.error" class="text-brand mb-2" role="alert">
      {{ pager.error }}
      <button class="btn btn-text" :disabled="pager.loading" @click="pager.retry().catch(() => {})">重试</button>
    </div>
    <div class="flex flex-wrap items-center justify-between gap-3">
      <span class="text-t3" aria-live="polite">共 {{ pager.total }} 条 · 第 {{ pager.page }} / {{ totalPages }} 页<span v-if="pager.loading"> · 加载中…</span></span>
      <div class="flex flex-wrap items-center gap-1">
        <select class="input !w-28 !h-8 !py-0 text-xs" aria-label="每页条数" :value="pager.pageSize" :disabled="pager.loading"
                @change="load({ pageSize: Number($event.target.value), reset: true })">
          <option v-for="size in [10, 20, 50]" :key="size" :value="size">{{ size }} 条 / 页</option>
        </select>
        <button class="btn btn-ghost !h-8" :disabled="pager.loading || pager.page <= 1" @click="load({ page: pager.page - 1 })">上一页</button>
        <button v-if="pages[0] > 1" class="btn btn-ghost !h-8" :disabled="pager.loading" @click="load({ page: 1 })">1</button>
        <span v-if="pages[0] > 2" class="text-t3">…</span>
        <button v-for="page in pages" :key="page" :class="['btn !h-8', page === pager.page ? 'btn-primary' : 'btn-ghost']"
                :aria-current="page === pager.page ? 'page' : undefined" :disabled="pager.loading" @click="load({ page })">{{ page }}</button>
        <span v-if="pages.at(-1) < totalPages - 1" class="text-t3">…</span>
        <button v-if="pages.at(-1) < totalPages" class="btn btn-ghost !h-8" :disabled="pager.loading" @click="load({ page: totalPages })">{{ totalPages }}</button>
        <button class="btn btn-ghost !h-8" :disabled="pager.loading || pager.page >= totalPages" @click="load({ page: pager.page + 1 })">下一页</button>
      </div>
    </div>
  </div>
</template>
