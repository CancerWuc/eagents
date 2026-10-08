// 管理面登录会话（admin-auth 01；login-topic §3/§5/§6）
// 双轨会话的本地态：eagent-session = {token, userId, displayName, roles, kind}
// 兼容通道：未登录会话时仍可用旧 X-Admin-Token（localStorage eagent-admin-token）
import { config } from './config.js'

const BASE = config.apiBase
const SESSION_KEY = 'eagent-session'

export function getSession() {
  try {
    return JSON.parse(localStorage.getItem(SESSION_KEY) || 'null')
  } catch {
    return null
  }
}

export function saveSession(session) {
  localStorage.setItem(SESSION_KEY, JSON.stringify(session))
}

export function clearSession() {
  localStorage.removeItem(SESSION_KEY)
}

export function hasAuth() {
  return Boolean(getSession()?.token) || Boolean(localStorage.getItem('eagent-admin-token'))
}

export function isAdmin() {
  const session = getSession()
  if (session?.roles?.includes('eagent_admin')) return true
  if (session?.kind === 'admin') return true
  // 旧通道：持有 X-Admin-Token 视作管理员（等价语义，见方案 §3.2）
  return !session && Boolean(localStorage.getItem('eagent-admin-token'))
}

export async function fetchAuthProviders() {
  // 一次请求拿齐登录方式（review P2）：{pluto: bool, local_login: bool}
  try {
    const resp = await fetch(`${BASE}/api/v1/admin/auth/providers`)
    if (!resp.ok) return { pluto: false, local_login: true }
    const body = await resp.json()
    return { pluto: Boolean(body.pluto), local_login: body.local_login !== false }
  } catch {
    return { pluto: false, local_login: true }
  }
}

export async function loginAdmin(username, password) {
  const resp = await fetch(`${BASE}/api/v1/admin/auth/login`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
  const body = await resp.json().catch(() => ({}))
  if (!resp.ok) {
    throw new Error(body.message || body.detail || `HTTP ${resp.status}`)
  }
  const session = { token: body.token, userId: body.username, displayName: body.username, roles: body.roles, kind: 'admin' }
  saveSession(session)
  return session
}

// Pluto 员工登录：拿 authorize_url 跳转；回跳后用 code+state 换本地会话
export async function fetchConsoleLogin() {
  const resp = await fetch(`${BASE}/api/v1/admin/console/login`)
  const body = await resp.json().catch(() => ({}))
  if (!resp.ok) throw new Error(body.message || body.detail || `HTTP ${resp.status}`)
  return body // {authorize_url, state}
}

export async function consoleCallback(code, state) {
  const resp = await fetch(`${BASE}/api/v1/admin/console/callback`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ code, state }),
  })
  const body = await resp.json().catch(() => ({}))
  if (!resp.ok) throw new Error(body.message || body.detail || `HTTP ${resp.status}`)
  const session = { token: body.token, userId: body.userId, displayName: body.displayName, roles: body.roles, kind: 'console' }
  saveSession(session)
  return session
}

export async function logout() {
  const token = getSession()?.token
  // 两个通道都要清：hasAuth() 只要有任一凭据即视为已登录，
  // 漏清旧通道会把用户留在「已登录但接口全 401」的空壳里。
  clearSession()
  localStorage.removeItem('eagent-admin-token')
  if (!token) return
  await fetch(`${BASE}/api/v1/admin/console/logout`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}` },
  }).catch(() => {})
}
