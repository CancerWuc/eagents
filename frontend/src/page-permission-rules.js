const LEVEL = { view: 1, manage: 2 }

export function permits(isAdmin, permissions, pageKey, requiredLevel = 'view') {
  if (pageKey === 'me') return true
  if (pageKey === 'permission' || pageKey === 'simulator') return Boolean(isAdmin)
  const level = permissions?.[pageKey]?.level
  return Boolean(isAdmin) || (LEVEL[level] || 0) >= LEVEL[requiredLevel]
}

export function indexPermissions(items) {
  return Object.fromEntries((items || []).map(item => [item.pageKey, item]))
}
