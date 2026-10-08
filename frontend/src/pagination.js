import { onBeforeUnmount, reactive, watch } from 'vue'
import { api } from './api.js'
import { createPagination } from './pagination-state.js'

export function useServerPagination(items, url, options = {}) {
  const state = reactive({})
  const pager = createPagination(
    params => api.get(listUrl(url(), { ...options.filters?.(), ...params })),
    state,
    result => { items.value = result.items; options.onResult?.(result) },
  )
  const load = pager.load
  pager.load = opts => load({ key: listUrl(url(), options.filters?.() || {}), ...opts })
  onBeforeUnmount(pager.invalidate)
  return pager
}

export function listUrl(url, params) {
  const [path, query = ''] = url.split('?')
  const search = new URLSearchParams(query)
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== '') search.set(key, value)
  }
  return `${path}?${search}`
}

export function watchPagination(pager, sources, delay = 0) {
  let timer
  watch(sources, () => {
    clearTimeout(timer)
    pager.invalidate()
    timer = setTimeout(() => pager.load({ reset: true }).catch(() => {}), delay)
  })
  onBeforeUnmount(() => clearTimeout(timer))
}
