export function visiblePages(page, totalPages) {
  const start = Math.max(1, Math.min(page - 2, totalPages - 4))
  return Array.from({ length: Math.min(5, totalPages) }, (_, i) => start + i)
}

export function createPagination(fetchPage, state = {}, apply = () => {}) {
  Object.assign(state, { page: 1, pageSize: 10, total: 0, loading: false, error: '' })
  let revision = 0
  let collection = ''
  let lastOptions = {}
  state.retry = () => state.load(lastOptions)
  state.invalidate = () => { revision++; state.loading = false }
  state.load = async (options = {}) => {
    lastOptions = { ...options }
    const current = ++revision
    const key = options.key ?? collection
    const changed = key !== collection
    const page = options.reset || changed ? 1 : (options.page ?? state.page)
    const pageSize = options.pageSize ?? state.pageSize
    state.loading = true
    state.error = ''
    try {
      const result = await fetchPage({ page, pageSize })
      if (current !== revision) return
      if (!result || !Array.isArray(result.items) || !Number.isInteger(result.total) || result.total < 0) {
        throw new Error('分页响应格式错误')
      }
      const lastPage = Math.max(1, Math.ceil(result.total / pageSize))
      if (page > lastPage) return await state.load({ ...options, page: lastPage, reset: false, key: collection })
      Object.assign(state, { page: result.page ?? page, pageSize: result.pageSize ?? pageSize, total: result.total })
      collection = key
      apply(result)
      return result
    } catch (error) {
      if (current !== revision) return
      state.error = error.message || '分页加载失败'
      throw error
    } finally {
      if (current === revision) state.loading = false
    }
  }
  return state
}
