// 全局通知（from_nekiro/12 §S5 / E1: 统一展示 code/message/traceId）
import { reactive } from 'vue'

export const toasts = reactive([])
let seq = 0

export function notify({ kind = 'error', code = '', message = '', traceId = '' }) {
  const id = ++seq
  toasts.push({ id, kind, code, message, traceId })
  // 成功提示看完即走，错误要留足时间抄 traceId
  setTimeout(() => dismiss(id), kind === 'success' ? 3000 : 9000)
  return id
}

/** 写操作成功反馈。没有这条通路时，保存成功和什么都没发生在界面上无法区分。 */
export function success(message) {
  return notify({ kind: 'success', message })
}

export function dismiss(id) {
  const i = toasts.findIndex((t) => t.id === id)
  if (i >= 0) toasts.splice(i, 1)
}
