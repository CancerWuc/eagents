// 启动期配置强校验（from_nekiro/12 §S5 / E3）
// 缺失/非法配置在模块加载时立即抛错（fail-fast），不允许带病启动。
const apiBase = import.meta.env.VITE_API_BASE ?? ''
const appName = import.meta.env.VITE_APP_NAME || 'eAgent 管理台'

const problems = []
if (typeof apiBase !== 'string') {
  problems.push('VITE_API_BASE 必须是字符串')
} else if (apiBase && !/^https?:\/\//i.test(apiBase)) {
  problems.push(`VITE_API_BASE=${apiBase} 非法: 必须以 http(s):// 开头，或留空表示同源部署`)
}
if (!appName || appName.length > 64) {
  problems.push('VITE_APP_NAME 缺失或超长（1-64 字符）')
}
if (problems.length) {
  // 不静默降级: 配置错误直接暴露给开发/运维
  throw new Error(
    `[eAgent 前端配置校验失败]\n${problems.map((p) => ` - ${p}`).join('\n')}` +
    '\n请检查 .env / 构建环境变量后重新构建。',
  )
}

export const config = {
  apiBase: apiBase.replace(/\/+$/, ''), // '' = 同源
  appName,
}
