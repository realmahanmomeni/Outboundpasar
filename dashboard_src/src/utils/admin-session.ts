import type { AdminDetails, getCurrentAdminResponse } from '@/service/api'

export const PLATFORM_OWNER_ROLE_ID = 1

/** Platform owner (builtin owner role). Fail closed when role data is missing. */
export function isPlatformOwner(admin: AdminDetails | null | undefined): boolean {
  const role = admin?.role
  if (!role || role.id == null) return false
  return role.id === PLATFORM_OWNER_ROLE_ID && role.is_owner === true
}

export function unwrapAdminDetails(value: unknown): AdminDetails | null {
  if (!value || typeof value !== 'object') return null

  const maybeWrapped = value as getCurrentAdminResponse
  if ('status' in maybeWrapped && maybeWrapped.status === 200 && maybeWrapped.data?.username) {
    return maybeWrapped.data
  }

  const maybeAdmin = value as AdminDetails
  if (typeof maybeAdmin.username === 'string') {
    return maybeAdmin
  }

  return null
}

export function selectCurrentAdminDetails(response: getCurrentAdminResponse): AdminDetails | null {
  if (response.status === 200) return response.data
  return null
}
