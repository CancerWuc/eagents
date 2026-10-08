<script setup>
import { computed } from 'vue'
import { statusDisplayName, toolDisplayName, toolTrail } from '../tool-display.js'

/**
 * 一次 Agent Team 调用的完整结果面板。
 *
 * 试运行和运行监控看的是同一种东西（一条 run_record），只是来源不同，
 * 所以共用这个组件——两边各写一份迟早会长歪。
 *
 * result 的形状见 MasterDetailView 的 runRecordToResult()：实时调用多出
 * toolbox（工具箱快照）与 inputRequired，从库里回看的记录这两项为空。
 */
const props = defineProps({
  result: { type: Object, required: true },
  toolLabels: { type: Object, default: () => ({}) },
})
defineEmits(['close', 'open-trace'])

const displayTool = (name) => toolDisplayName(name, props.toolLabels)
const routedTrail = computed(() =>
  toolTrail(props.result.routedServices || [], props.toolLabels))

const statusClass = (st) => ({
  SUCCEEDED: 'badge badge-ok', FAILED: 'badge badge-err',
  LIMIT_REACHED: 'badge badge-warn', REJECTED: 'badge badge-warn',
  CANCELED: 'badge badge-gray',
}[st] || 'badge badge-gray')

// AgentScope 的 ToolResultState 序列化后形如 'ToolResultState.SUCCESS'，
// 也可能是裸字符串 'success'，两种都要认。
const toolStateClass = (st) =>
  String(st).toUpperCase().includes('SUCCESS') ? 'badge badge-ok' : 'badge badge-err'
const toolStateText = (st) =>
  String(st).toUpperCase().includes('SUCCESS') ? '成功' : '失败'

/**
 * 耗时构成（后端 run_timing）。
 *
 * 存在的理由：总耗时是几十秒、账本里的子 Agent 调用却只有几十毫秒，
 * 光看这两个数没法判断慢在哪。这里把一次运行拆成「模型在想」「工具在跑」
 * 「其余开销」三段。老记录没有 timing_json，整块自动隐藏。
 */
const timing = computed(() => props.result.timing || null)
const fmtMs = (ms) => ms == null ? '—'
  : (ms >= 1000 ? (ms / 1000).toFixed(1) + ' s' : Math.round(ms) + ' ms')
const pct = (ms) => {
  const total = timing.value?.totalMs || 0
  return total > 0 ? Math.round((ms || 0) / total * 100) : 0
}
const timingParts = computed(() => {
  const t = timing.value
  if (!t || !t.totalMs) return []
  return [
    {
      key: 'llm', label: '模型推理', ms: t.llmMs,
      badge: 'badge badge-brand', bar: 'bg-brand',
      hint: `${t.llmCalls} 轮往返，最慢一轮 ${fmtMs(t.llmMaxMs)}`,
    },
    {
      key: 'tool', label: '工具调用', ms: t.toolMs,
      badge: 'badge badge-ok', bar: 'bg-up',
      hint: `${t.toolCalls} 次调用；并发重叠的部分只计一次`,
    },
    {
      key: 'other', label: '其余开销', ms: t.otherMs,
      badge: 'badge badge-gray', bar: 'bg-t3',
      hint: '提示词组装、上下文拼接、落库等框架开销',
    },
  ].filter((p) => p.ms > 0)
})
const timingHint = computed(() => {
  const t = timing.value
  if (!t || !t.totalMs) return ''
  if (pct(t.llmMs) >= 60) {
    return `模型推理占 ${pct(t.llmMs)}%：${t.llmCalls} 轮串行往返、最慢一轮 `
      + `${fmtMs(t.llmMaxMs)}。提速要从减轮次、缩上下文、开流式入手，`
      + `跟下游服务快慢无关。`
  }
  if (pct(t.toolMs) >= 60) {
    return `工具调用占 ${pct(t.toolMs)}%：慢在被调的子 Agent 或知识库，`
      + `展开逐段明细能看出是哪一个。`
  }
  return `模型 ${pct(t.llmMs)}% · 工具 ${pct(t.toolMs)}% · `
    + `其余 ${pct(t.otherMs)}%（提示词组装、上下文拼接、落库等）。`
})
const spanLabel = (span) =>
  span.phase === 'llm' ? span.name : displayTool(span.name)
</script>

<template>
  <div class="card">
    <div class="flex items-center gap-3 flex-wrap mb-4">
      <span :class="statusClass(result.status)" :title="result.status">{{ statusDisplayName(result.status) }}</span>
      <span v-if="result.inputRequired" class="badge badge-warn"
        title="模型判定信息不足，已向用户追问">需要补充信息</span>
      <span v-if="result.errorCode" class="badge badge-err mono">{{ result.errorCode }}</span>
      <span class="text-[12px] text-t2">运行耗时</span>
      <span class="mono text-[12px] text-t2">{{ result.elapsedMs == null ? '—' : result.elapsedMs + ' ms' }}</span>
      <button v-if="result.traceId"
        class="ml-auto flex items-center gap-2 rounded-lg border border-brand bg-brand-light px-3 py-2 text-left hover:brightness-95"
        title="跳转到调用链路追踪并自动查询"
        @click="$emit('open-trace', result.traceId)">
        <svg class="w-4 h-4 text-brand shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
          <path d="M5 12h14M13 6l6 6-6 6"/><circle cx="5" cy="12" r="2"/>
        </svg>
        <span>
          <span class="block text-[12px] font-semibold text-brand">查看完整调用链</span>
          <span class="block mono text-[11px] text-t2">{{ result.traceId }}</span>
        </span>
      </button>
      <span v-else class="ml-auto"></span>
      <button class="btn btn-text" title="收起结果" @click="$emit('close')">✕</button>
    </div>

    <div class="bg-canvas rounded-lg px-4 py-3 text-sm leading-7 whitespace-pre-wrap">{{ result.output }}</div>

    <!-- 失败原因：排障时最想看的一行，之前只在库里躺着 -->
    <div v-if="result.errorDetail" class="mt-3 border border-line rounded-lg px-3 py-2">
      <div class="text-[13px] text-t2 mb-1">失败详情</div>
      <div class="mono text-[12px] text-brand whitespace-pre-wrap break-all">{{ result.errorDetail }}</div>
    </div>

    <div class="flex items-center gap-2 flex-wrap mt-4">
      <span class="text-[13px] text-t2">调用轨迹：</span>
      <template v-if="routedTrail.length">
        <span v-for="item in routedTrail" :key="item.index" class="badge badge-brand"
          :title="item.name">
          {{ item.index }}. {{ item.label }}
        </span>
      </template>
      <span v-else class="text-t3 text-[13px]">没有调用任何工具（模型直接回答或先行追问）</span>
    </div>

    <!-- 耗时构成：排性能问题的第一屏，比逐段明细更常用，所以不折叠 -->
    <div v-if="timingParts.length" class="mt-4">
      <div class="flex items-center gap-2 flex-wrap">
        <span class="text-[13px] text-t2">耗时构成：</span>
        <span v-for="p in timingParts" :key="p.key" :class="p.badge" :title="p.hint">
          {{ p.label }} {{ fmtMs(p.ms) }} · {{ pct(p.ms) }}%
        </span>
      </div>
      <div class="mt-2 flex h-2 w-full overflow-hidden rounded bg-canvas">
        <div v-for="p in timingParts" :key="p.key" :class="p.bar"
          :style="{ width: pct(p.ms) + '%' }"
          :title="`${p.label} ${fmtMs(p.ms)}`"></div>
      </div>
      <p class="text-t3 text-[12px] mt-2">{{ timingHint }}</p>

      <details v-if="timing.spans && timing.spans.length" class="mt-2">
        <summary class="cursor-pointer text-[13px] text-t2 select-none">
          逐段明细（{{ timing.spans.length }} 段，按发生顺序）
        </summary>
        <div class="mt-2 border border-line rounded-lg">
          <div v-for="(s, i) in timing.spans" :key="i"
            class="flex items-center gap-3 px-3 py-1.5 border-b border-line last:border-b-0">
            <span class="mono text-[11px] text-t3 w-12 shrink-0 text-right"
              title="相对本次运行开始的偏移">+{{ (s.atMs / 1000).toFixed(1) }}s</span>
            <span :class="s.phase === 'llm' ? 'badge badge-brand' : 'badge badge-ok'">
              {{ s.phase === 'llm' ? '模型' : '工具' }}
            </span>
            <span class="text-[13px] truncate">{{ spanLabel(s) }}</span>
            <span v-if="s.failed" class="badge badge-err">失败</span>
            <span class="mono text-[12px] text-t2 ml-auto shrink-0">{{ fmtMs(s.ms) }}</span>
          </div>
        </div>
        <p v-if="timing.droppedSpans" class="text-t3 text-[12px] mt-2">
          还有 {{ timing.droppedSpans }} 段未记录（单次运行的明细上限 200 段）。
        </p>
      </details>
    </div>

    <details v-if="result.toolCalls.length" class="mt-4">
      <summary class="cursor-pointer text-[13px] text-t2 select-none">
        工具返回原文（{{ result.toolCalls.length }} 次，已脱敏截断）
      </summary>
      <div v-for="(c, i) in result.toolCalls" :key="i"
        class="mt-2 border border-line rounded-lg px-3 py-2">
        <div class="flex items-center gap-2 mb-1.5">
          <span class="text-[13px] font-medium">{{ displayTool(c.tool) }}</span>
          <span v-if="displayTool(c.tool) !== c.tool" class="mono text-[11px] text-t3">{{ c.tool }}</span>
          <span :class="toolStateClass(c.state)" :title="c.state">{{ toolStateText(c.state) }}</span>
        </div>
        <div class="mono text-[12px] text-t2 whitespace-pre-wrap break-all">{{ c.output }}</div>
      </div>
    </details>

    <p v-if="result.restored" class="text-t3 text-[12px] mt-4">
      这是从运行记录里回看的历史结果。工具箱快照和「需要补充信息」标记不入库，
      所以这两项看不到；答案正文与工具返回原文都是当时真实存下来的。
    </p>

    <details v-if="result.toolbox" class="mt-4">
      <summary class="cursor-pointer text-[13px] text-t2 select-none">
        这次调用模型手上有哪些工具（A2A {{ result.toolbox.a2a.length }} 个 ·
        知识库 {{ result.toolbox.kb.length }} 个<template
          v-if="Object.keys(result.toolbox.unavailable).length">
          · 不可用 {{ Object.keys(result.toolbox.unavailable).length }} 个</template>）
      </summary>
      <p class="text-t3 text-[12px] mt-2">
        答案不对时先看这里：工具压根不在箱子里 = 权限或发布链的问题；
        在箱子里但模型没选 = 提示词或工具描述的问题。
      </p>
      <div v-if="!result.toolbox.a2a.length && !result.toolbox.kb.length"
        class="text-brand text-[13px] mt-2">
        工具箱是空的，这次调用模型只能凭自己回答。
      </div>
      <div v-for="t in result.toolbox.a2a" :key="t.toolName"
        class="mt-2 border border-line rounded-lg px-3 py-2">
        <div class="flex items-center gap-2 flex-wrap">
          <span class="text-[13px] font-medium">{{ displayTool(t.toolName) }}</span>
          <span class="badge badge-gray">版本 {{ t.agentVersion }}</span>
        </div>
        <div class="text-t2 text-[12px] mt-1">{{ t.toolDescription }}</div>
        <div class="mono text-t3 text-[11px] mt-1">{{ t.toolName }} · {{ t.serviceSlug }}</div>
      </div>
      <div v-for="t in result.toolbox.kb" :key="t.name"
        class="mt-2 border border-line rounded-lg px-3 py-2">
        <span class="text-[13px] font-medium">{{ displayTool(t.name) }}</span>
        <div class="text-t2 text-[12px] mt-1">{{ t.description }}</div>
        <div class="mono text-t3 text-[11px] mt-1">{{ t.name }}</div>
      </div>
      <div v-for="(reason, name) in result.toolbox.unavailable" :key="name"
        class="mt-2 border border-line rounded-lg px-3 py-2">
        <span class="mono text-[13px] font-medium">{{ name }}</span>
        <span class="badge badge-err mono ml-2">{{ reason }}</span>
        <div class="text-t2 text-[12px] mt-1">已绑定但解析不到可用 Release，未进入工具箱</div>
      </div>
    </details>

    <details class="mt-4">
      <summary class="cursor-pointer text-[12px] text-t3 select-none">技术标识与运行上下文</summary>
      <div class="mono text-[12px] text-t3 mt-2 flex gap-4 flex-wrap">
        <span>run {{ result.runId }}</span>
        <span>身份 {{ result.asUser }}</span>
        <span>上下文 {{ result.contextId }}</span>
        <span>提示词 {{ result.promptVersionId || '（未发布，用内置默认）' }}</span>
        <span v-if="result.traceId">trace {{ result.traceId }}</span>
      </div>
    </details>
  </div>
</template>
