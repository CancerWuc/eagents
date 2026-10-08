const BUILTIN_LABELS = {
  hr: '人事服务',
  hr_profile: '员工档案',
  finance: '财务服务',
  finance_budget: '预算检查',
  weather: '天气服务',
  weather_risk: '天气风险',
  travel_inventory: '差旅库存报价',
  compliance_review: '合规审查',
  approval_workflow: '审批流',
  travel_booking: '差旅预订',
  notification: '通知',
}

export function toolDisplayName(name, labels = {}) {
  if (!name) return '未知工具'
  if (labels[name]) return labels[name]
  if (BUILTIN_LABELS[name]) return BUILTIN_LABELS[name]
  if (name.startsWith('kb_search_')) return '知识库搜索'
  if (name.startsWith('kb_read_')) return '知识库页面读取'
  if (name.startsWith('kb_graph_')) return '知识图谱查询'
  return name
}

/** 保留实际调用顺序，并为同一工具的重复调用标出“第 N 次”。 */
export function toolTrail(names = [], labels = {}) {
  const totals = {}
  const seen = {}
  for (const name of names) totals[name] = (totals[name] || 0) + 1
  return names.map((name, index) => {
    seen[name] = (seen[name] || 0) + 1
    const repeat = totals[name] > 1 ? `（第 ${seen[name]} 次）` : ''
    return {
      index: index + 1,
      name,
      label: `${toolDisplayName(name, labels)}${repeat}`,
    }
  })
}

export function toolTrailText(names = [], labels = {}) {
  return toolTrail(names, labels).map(item => item.label).join(' → ')
}

export function statusDisplayName(status) {
  return {
    SUCCEEDED: '成功',
    FAILED: '失败',
    RUNNING: '运行中',
    LIMIT_REACHED: '达到轮数上限',
    REJECTED: '已拒绝',
    CANCELED: '已取消',
  }[status] || status
}
