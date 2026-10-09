<script setup>
import { h } from 'vue'
import { fmtDateTime } from '../format.js'
import { statusDisplayName, toolTrailText } from '../tool-display.js'

const props = defineProps({
  groups: { type: Array, required: true },
  mode: { type: String, default: 'history' },
  selectedRun: Object,
  toolLabels: { type: Object, default: () => ({}) },
})
const emit = defineEmits(['select'])
const formatToolTrail = names => toolTrailText(names || [], props.toolLabels) || '—'
const statusClass = status => ({
  SUCCEEDED: 'badge badge-ok', FAILED: 'badge badge-err',
  LIMIT_REACHED: 'badge badge-warn', REJECTED: 'badge badge-warn',
  CANCELED: 'badge badge-gray',
}[status] || 'badge badge-gray')
function sourceLabel(contextId) {
  if (String(contextId).startsWith('testrun-')) return '试运行'
  if (String(contextId).startsWith('perf-')) return '性能测试'
  return 'A2A'
}
// 监控列较多，需要横向滚动；历史表保留原来的直接父子结构。
const TableContainer = (_, { slots }) => props.mode === 'runs'
  ? h('div', { class: 'overflow-x-auto' }, slots.default()) : slots.default()
</script>

<template>
  <div v-for="group in groups" :key="group.contextId"
    class="border border-line rounded-lg mb-3 last:mb-0 overflow-hidden">
    <div class="flex items-center gap-2 flex-wrap px-3 py-2 bg-canvas">
      <span v-if="mode === 'runs'" class="badge badge-gray">{{ sourceLabel(group.contextId) }}</span>
      <span class="text-[13px] font-medium">
        {{ group.turns.length > 1 ? `${group.turns.length} 轮对话` : '单轮' }}
      </span>
      <span class="mono text-[12px] text-t3" :title="group.contextId">{{ group.contextId }}</span>
      <span class="mono text-[12px] text-t3" :class="{ 'ml-auto': mode === 'runs' }">{{ fmtDateTime(group.latestAt, '') || (mode === 'history' ? '刚刚' : '') }}</span>
      <slot name="group-actions" :group="group" />
    </div>
    <TableContainer>
      <table class="tbl">
        <tbody>
          <tr v-for="(entry, index) in group.turns" :key="mode === 'history' ? entry.r.runId || index : entry.r.runId"
            class="cursor-pointer" :class="selectedRun && selectedRun.runId === entry.r.runId ? 'bg-canvas' : ''"
            @click="emit('select', entry)">
            <td class="w-8 mono text-[12px] text-t3">{{ index + 1 }}</td>
            <td class="max-w-md truncate" :title="entry.q">{{ entry.q }}</td>
            <td class="w-28">
              <span :class="statusClass(entry.r.status)" :title="entry.r.status">{{ statusDisplayName(entry.r.status) }}</span>
              <span v-if="mode === 'history' && entry.r.inputRequired" class="badge badge-warn ml-1.5">追问</span>
            </td>
            <td v-if="mode === 'runs'" class="w-40 mono text-[12px] text-brand">{{ entry.r.errorCode || '' }}</td>
            <td class="text-[12px] max-w-xl truncate" :title="formatToolTrail(entry.r.routedServices)">{{ formatToolTrail(entry.r.routedServices) }}</td>
            <td v-if="mode === 'runs'" class="mono text-[12px] text-t2 w-28">{{ entry.r.asUser }}</td>
            <td class="mono text-[12px] w-24">{{ entry.r.elapsedMs == null ? '—' : entry.r.elapsedMs + ' ms' }}</td>
            <td class="mono text-[12px] text-t3 w-20">{{ fmtDateTime(entry.r.startedAt, '').slice(11) || (mode === 'history' ? '刚刚' : '') }}</td>
            <td v-if="mode === 'runs'" class="w-16 whitespace-nowrap" @click.stop>
              <slot name="row-actions" :entry="entry" />
            </td>
          </tr>
        </tbody>
      </table>
    </TableContainer>
  </div>
</template>
