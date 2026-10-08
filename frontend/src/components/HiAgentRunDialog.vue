<script setup>
import { ref } from 'vue'
import { sendA2aMessage, cancelA2aTask, reconnectA2aTask } from '../a2a-client.js'
const props = defineProps({ service: Object })
defineEmits(['close'])
const text = ref('')
const parameters = ref('{}')
const fileReferences = ref('[]')
const result = ref(null)
const error = ref('')
const busy = ref(false)
async function send(optionId) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    const previous = result.value
    const files = JSON.parse(fileReferences.value)
    if (!Array.isArray(files)) throw new Error('文件引用 JSON 必须是数组')
    result.value = await sendA2aMessage({ slug: props.service.slug, text: text.value,
      contextId: previous?.contextId,
      taskId: previous?.inputRequired ? previous.taskId : undefined,
      inputs: JSON.parse(parameters.value),
      files,
      interactionResponse: optionId ? { interactionId: previous.interaction.interactionId, selectedOptionIds: [optionId] } : undefined,
      onProgress: update => { result.value = update }, consolePage: 'registry' })
    text.value = ''
    fileReferences.value = '[]'
  } catch (e) {
    if (result.value?.taskId && ['SUBMITTED', 'WORKING'].includes(result.value.status)) {
      try { result.value = await reconnectA2aTask(props.service.slug, result.value.taskId, update => { result.value = update }, 'registry'); return }
      catch (reconnectError) { error.value = `${e.message}；重连失败：${reconnectError.message}` }
    } else error.value = e.message
  }
  finally { busy.value = false }
}
async function stop() {
  if (!result.value?.taskId) return
  try { result.value = await cancelA2aTask(props.service.slug, result.value.taskId, 'registry') }
  catch (e) { error.value = e.message }
}
function newConversation() {
  result.value = null
  fileReferences.value = '[]'
}
function cardPayload(card) {
  return typeof card.payload === 'string'
    ? card.payload
    : JSON.stringify(card.payload, null, 2)
}
</script>

<template>
  <div class="fixed inset-0 z-50 bg-black/30 flex items-center justify-center p-6">
    <section class="bg-white rounded-xl shadow-xl w-full max-w-2xl p-6 max-h-[90vh] overflow-auto" role="dialog" aria-modal="true" aria-label="HiAgent 试运行">
      <div class="flex justify-between items-center mb-4">
        <h2 class="font-semibold">{{ service.display_name || service.slug }} · 试运行</h2>
        <button class="btn btn-ghost" :disabled="busy" @click="$emit('close')">关闭</button>
      </div>
      <label class="block text-sm mb-2">完整业务 Inputs（JSON）</label>
      <textarea v-model="parameters" class="input w-full mono mb-4" rows="3" :disabled="busy" />
      <label class="block text-sm mb-2">文件引用（JSON 数组，本轮有效）</label>
      <textarea v-model="fileReferences" class="input w-full mono mb-2" rows="4" :disabled="busy"
        placeholder='[{"url":"https://hiagent.example/upload/a.pdf","filename":"a.pdf","mediaType":"application/pdf","path":"upload/a.pdf","size":1024}]' />
      <p class="text-xs text-t3 mb-4">仅填写已上传到当前 HiAgent 同源地址的文件。卡片提交前请更新完整 Inputs；如仍需文件，请重新填写。</p>
      <label class="block text-sm mb-2">{{ result?.inputRequired ? '卡片提交将复用首次问题（无需填写）' : '用户问题' }}</label>
      <textarea v-model="text" class="input w-full mb-4" rows="3" :disabled="busy || result?.inputRequired" />
      <div class="flex gap-2">
        <button class="btn btn-primary" :disabled="busy || !text.trim()" @click="send()">{{ busy ? '执行中…' : '发送' }}</button>
        <button class="btn btn-ghost" :disabled="busy" @click="newConversation">新对话</button>
        <button v-if="busy && result?.taskId" class="btn btn-ghost" @click="stop">停止任务</button>
      </div>
      <p v-if="error" class="text-brand mt-4">{{ error }}</p>
      <div v-if="result" class="mt-5 border-t border-line pt-4">
        <p class="text-xs text-t3 mb-2">{{ result.status }}</p>
        <p class="whitespace-pre-wrap">{{ result.output }}</p>
        <div v-if="result.cards?.length" class="space-y-3 mt-4">
          <div v-for="(card, index) in result.cards" :key="index" class="rounded-lg border border-line bg-slate-50 p-3">
            <p class="text-xs text-t3 mb-2">卡片 · {{ card.sourceFormat }}</p>
            <pre class="text-sm whitespace-pre-wrap break-words overflow-auto">{{ cardPayload(card) }}</pre>
          </div>
        </div>
        <p v-if="result.errorDetail" class="text-brand">{{ result.errorDetail }}</p>
        <div v-if="result.inputRequired && result.interaction" class="flex flex-wrap gap-2 mt-4">
          <button v-for="option in result.interaction.options" :key="option.id" class="btn btn-ghost" :disabled="busy" @click="send(option.id)">{{ option.label }}</button>
        </div>
      </div>
    </section>
  </div>
</template>
