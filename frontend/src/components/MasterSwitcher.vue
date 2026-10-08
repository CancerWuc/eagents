<script setup>
// Agent Team 切换器：点击展开下拉，展示全部可管理的 COMPOSITE Team
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'

const props = defineProps({
  masters: { type: Array, default: () => [] }, // listAgents().composite
  currentId: String,
})
const emit = defineEmits(['select'])

const open = ref(false)
const root = ref(null)
const keyword = ref('')
const current = computed(() => props.masters.find(m => m.master_id === props.currentId))
const filtered = computed(() => {
  const k = keyword.value.trim().toLowerCase()
  if (!k) return props.masters
  return props.masters.filter(m =>
    (m.display_name || '').toLowerCase().includes(k) || (m.slug || '').toLowerCase().includes(k))
})

function pick(id) {
  open.value = false
  keyword.value = ''
  if (id !== props.currentId) emit('select', id)
}
function onDocClick(e) {
  if (root.value && !root.value.contains(e.target)) open.value = false
}
onMounted(() => document.addEventListener('click', onDocClick))
onBeforeUnmount(() => document.removeEventListener('click', onDocClick))
</script>

<template>
  <div ref="root" class="relative">
    <button type="button"
      class="border border-line rounded-lg px-3 h-9 flex items-center gap-2 bg-white min-w-56 max-w-72 justify-between hover:border-brand transition-colors"
      @click.stop="open = !open">
      <span class="truncate font-medium">{{ current?.display_name || '选择 Agent Team' }}</span>
      <svg class="w-3.5 h-3.5 text-t3 shrink-0 transition-transform" :class="open ? 'rotate-180' : ''"
        viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 9l6 6 6-6"/></svg>
    </button>

    <div v-if="open" class="absolute left-0 top-11 z-30 w-80 bg-white border border-line rounded-xl shadow-lg py-1.5">
      <div class="px-3.5 pb-1.5 flex items-center justify-between gap-2">
        <span class="text-xs text-t3 whitespace-nowrap">共 {{ masters.length }} 个（按创建时间倒序）</span>
        <input v-model="keyword" class="input !py-1 !text-xs w-36" placeholder="搜索名称 / ID" @click.stop />
      </div>
      <div class="max-h-72 overflow-y-auto">
        <div v-for="m in filtered" :key="m.master_id"
          class="flex items-center gap-2.5 px-3.5 py-2.5 cursor-pointer hover:bg-canvas"
          :class="m.master_id === currentId ? 'bg-brand-light/50' : ''"
          @click="pick(m.master_id)">
          <div class="flex-1 min-w-0">
            <div class="font-medium text-sm truncate">{{ m.display_name }}</div>
            <div class="mono text-xs truncate">{{ m.slug }} · /a2a/{{ m.slug }}/</div>
          </div>
          <svg v-if="m.master_id === currentId" class="w-4 h-4 text-brand shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6L9 17l-5-5"/></svg>
        </div>
        <div v-if="!filtered.length" class="px-3.5 py-3 text-t3 text-sm">
          {{ masters.length ? '没有匹配「' + keyword + '」的 Agent Team' : '暂无可管理的 Agent Team' }}
        </div>
      </div>
    </div>
  </div>
</template>
