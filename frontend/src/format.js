// 时间展示统一口径：后端返回 ISO（含 T 与微秒），界面统一转
// `YYYY-MM-DD HH:mm:ss`；Release / Installation 等此前直接裸展示 ISO。
export function fmtDateTime(t) {
  if (!t) return '—'
  return String(t).replace('T', ' ').slice(0, 19)
}
