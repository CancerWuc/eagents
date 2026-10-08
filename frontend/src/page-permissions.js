import { reactive } from 'vue'
import { api } from './api.js'
import { indexPermissions, permits } from './page-permission-rules.js'

export const pageAccess = reactive({
  loaded: false,
  isAdmin: false,
  permissions: {},
})

export async function refreshPageAccess() {
  const result = await api.myPagePermissions()
  pageAccess.isAdmin = Boolean(result.isAdmin)
  pageAccess.permissions = indexPermissions(result.permissions)
  pageAccess.loaded = true
  return pageAccess
}

export function canViewPage(pageKey) {
  return permits(pageAccess.isAdmin, pageAccess.permissions, pageKey, 'view')
}

export function canManagePage(pageKey) {
  return permits(pageAccess.isAdmin, pageAccess.permissions, pageKey, 'manage')
}

export function clearPageAccess() {
  pageAccess.loaded = false
  pageAccess.isAdmin = false
  pageAccess.permissions = {}
}
