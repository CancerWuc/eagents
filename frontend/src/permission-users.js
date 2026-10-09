export function activeUserCandidates(users, query, { exclude = [], limit = 8 } = {}) {
  const q = query.trim().toLowerCase()
  const picked = new Set(exclude)
  return users
    .filter(user => user.status === 'active' && !picked.has(user.userId))
    .filter(user => !q || [user.userId, user.displayName, user.department]
      .some(value => (value || '').toLowerCase().includes(q)))
    .map(user => ({ cap_user_name: user.userId, display_name: user.displayName, department: user.department }))
    .slice(0, limit)
}
