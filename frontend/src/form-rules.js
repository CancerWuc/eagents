// 与后端 SLUG_PATTERN 一致；重复标识由调用页面按当前列表检查。
export function slugValidationError(raw) {
  const value = raw.trim()
  return value && !/^[a-z0-9][a-z0-9-]{1,63}$/.test(value)
    ? '2–64 位，小写字母或数字开头，仅含小写字母、数字与短横线' : ''
}

export function httpUrlError(raw) {
  const value = raw.trim()
  return value && !/^https?:\/\/.+/i.test(value) ? '需以 http:// 或 https:// 开头' : ''
}
