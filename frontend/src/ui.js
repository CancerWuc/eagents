// 页面内模态交互（window.prompt / window.confirm 在内嵌浏览器不可用，
// 且样式不可控；被浏览器挡掉时会静默返回，表现为「点了没反应」）
const promptHandlers = new Set()
const confirmHandlers = new Set()

export function onModalPrompt(fn) {
  promptHandlers.add(fn)
  return () => promptHandlers.delete(fn)
}

export function promptText(message, defaultValue = '') {
  return new Promise((resolve) => {
    if (!promptHandlers.size) {
      resolve(null)
      return
    }
    promptHandlers.forEach(fn => fn({ message, defaultValue, resolve }))
  })
}

export function onModalConfirm(fn) {
  confirmHandlers.add(fn)
  return () => confirmHandlers.delete(fn)
}

/** 二次确认。未挂载渲染器时 fail-closed 返回 false（宁可不执行，不要误执行）。 */
export function confirmAction(message, { okText = '确认', danger = false } = {}) {
  return new Promise((resolve) => {
    if (!confirmHandlers.size) {
      resolve(false)
      return
    }
    confirmHandlers.forEach(fn => fn({ message, okText, danger, resolve }))
  })
}
