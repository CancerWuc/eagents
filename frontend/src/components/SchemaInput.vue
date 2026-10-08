<script setup>
import { computed, ref, watch } from 'vue'
import { fieldType, initialValue } from '../simulator.js'
const props = defineProps({ schema: { type: [Object, Boolean], default: () => ({}) }, modelValue: null, label: { type: String, default: '参数' }, depth: { type: Number, default: 0 } })
const emit = defineEmits(['update:modelValue'])
const type = computed(() => {
  const candidate = props.depth > 10 ? 'json' : fieldType(props.schema)
  if (candidate === 'array' && !Array.isArray(props.modelValue)) return 'json'
  if (candidate === 'object' && (!props.modelValue || typeof props.modelValue !== 'object' || Array.isArray(props.modelValue))) return 'json'
  return candidate
})
const nullable = computed(() => Array.isArray(props.schema?.type) && props.schema.type.includes('null'))
const draft = ref('')
const jsonError = ref('')
watch(() => props.modelValue, value => { draft.value = JSON.stringify(value, null, 2) ?? ''; jsonError.value = '' }, { immediate: true, deep: true })
function parseDraft(event) {
  draft.value = event.target.value
  try { emit('update:modelValue', JSON.parse(draft.value)); jsonError.value = ''; event.target.setCustomValidity('') }
  catch { jsonError.value = '请输入有效 JSON'; event.target.setCustomValidity(jsonError.value) }
}
function changeKey(key, value, include = true) {
  const next = { ...(props.modelValue || {}) }
  if (include) Object.defineProperty(next, key, { value, writable: true, configurable: true, enumerable: true })
  else delete next[key]
  emit('update:modelValue', next)
}
function changeItem(index, value) {
  const next = [...props.modelValue]; next[index] = value; emit('update:modelValue', next)
}
</script>

<template>
  <div class="schema-input min-w-0">
    <label v-if="nullable" class="flex gap-2 text-xs text-t2 mb-2"><input type="checkbox" :checked="modelValue === null" @change="emit('update:modelValue', $event.target.checked ? null : initialValue({ ...schema, type: schema.type.filter(t => t !== 'null')[0] }))">显式传递 null</label>
    <template v-if="modelValue !== null || !nullable">
      <label v-if="type === 'enum'" class="block"><span class="sr-only">{{ label }}</span><select class="input w-full" :value="JSON.stringify(modelValue)" @change="emit('update:modelValue', JSON.parse($event.target.value))"><option disabled value="">请选择</option><option v-for="(value, i) in schema.enum" :key="i" :value="JSON.stringify(value)">{{ typeof value === 'string' ? value : JSON.stringify(value) }}</option></select></label>
      <div v-else-if="type === 'object'" class="space-y-3">
        <div v-for="(child, key) in schema.properties || {}" :key="key" class="border-l-2 border-line pl-3">
          <label class="flex items-center gap-2 text-sm mb-1"><input type="checkbox" :aria-label="`提供 ${child.title || key}`" :checked="Object.hasOwn(modelValue || {}, key)" @change="changeKey(key, initialValue(child), $event.target.checked)"><span>{{ child.title || key }} <span v-if="schema.required?.includes(key)" class="text-brand">* 必填</span><span v-else class="text-t3 text-xs">选填</span></span></label>
          <p v-if="child.description" class="text-xs text-t3 mb-2">{{ child.description }}</p>
          <SchemaInput v-if="Object.hasOwn(modelValue || {}, key)" :schema="child" :label="String(child.title || key)" :model-value="modelValue[key]" :depth="depth + 1" @update:model-value="changeKey(key, $event)" />
          <button v-if="Object.hasOwn(child, 'default')" type="button" class="btn btn-text text-xs mt-1" @click="changeKey(key, child.default)">采用建议值：{{ JSON.stringify(child.default) }}</button>
        </div>
        <p v-if="!Object.keys(schema.properties || {}).length" class="text-xs text-t3">无已声明字段；复杂对象可在 JSON 模式中填写。</p>
      </div>
      <div v-else-if="type === 'array'" class="space-y-2">
        <div v-for="(item, i) in modelValue || []" :key="i" class="flex gap-2 items-start"><div class="flex-1 min-w-0"><SchemaInput :schema="schema.items || {}" :label="`${label} 第 ${i + 1} 项`" :model-value="item" :depth="depth + 1" @update:model-value="changeItem(i, $event)" /></div><button type="button" class="btn btn-ghost" :aria-label="`删除第 ${i + 1} 项`" @click="emit('update:modelValue', modelValue.filter((_, n) => n !== i))">移除</button></div>
        <button type="button" class="btn btn-ghost text-xs" @click="emit('update:modelValue', [...(modelValue || []), initialValue(schema.items || {})])">＋ 添加一项</button>
      </div>
      <label v-else-if="type === 'boolean'" class="flex gap-2 text-sm"><input type="checkbox" :checked="modelValue" @change="emit('update:modelValue', $event.target.checked)">{{ label }}：{{ modelValue ? '是' : '否' }}</label>
      <input v-else-if="type === 'number' || type === 'integer'" class="input w-full" :aria-label="label" type="number" :step="type === 'integer' ? 1 : 'any'" :value="modelValue" @input="emit('update:modelValue', $event.target.value === '' ? '' : Number($event.target.value))">
      <input v-else-if="type === 'string'" class="input w-full" :aria-label="label" :value="modelValue" @input="emit('update:modelValue', $event.target.value)">
      <div v-else><textarea class="input w-full font-mono" :aria-label="`${label} JSON`" :value="draft" rows="4" @input="parseDraft" /><p class="text-xs text-t3">结构化 JSON，完整约束由服务端校验。</p><p v-if="jsonError" role="alert" class="text-brand text-xs">{{ jsonError }}</p></div>
    </template>
  </div>
</template>
