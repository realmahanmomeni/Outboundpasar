import { orvalFetcher } from './http'

export interface HWIDManagementSummary {
  device_count: number
  users_with_devices: number
  blocked_devices: number
}

export interface HWIDManagementUserBrief {
  user_id: number
  username: string
  active_devices: number
  last_activity_at?: string | null
  device_status: string
}

export interface HWIDManagementUserListResponse {
  users: HWIDManagementUserBrief[]
  total: number
}

export interface HWIDManagementDevice {
  id: number
  hwid: string
  device_os?: string | null
  os_version?: string | null
  device_model?: string | null
  created_at: string
  last_used_at: string
}

export interface HWIDManagementUserDetail {
  user: {
    user_id: number
    username: string
    hwid_limit?: number | null
    device_count: number
    last_activity_at?: string | null
    status: string
  }
  devices: HWIDManagementDevice[]
}

export function getHwidManagementSummary() {
  return orvalFetcher<HWIDManagementSummary>('/api/hwid/summary')
}

export function listHwidManagementUsers(params?: { offset?: number; limit?: number }) {
  const search = new URLSearchParams()
  if (params?.offset != null) search.set('offset', String(params.offset))
  if (params?.limit != null) search.set('limit', String(params.limit))
  const q = search.toString()
  return orvalFetcher<HWIDManagementUserListResponse>(`/api/hwid/users${q ? `?${q}` : ''}`)
}

export function getHwidManagementUserDetail(userId: number) {
  return orvalFetcher<HWIDManagementUserDetail>(`/api/hwid/users/${userId}`)
}

export function revokeHwidDevice(deviceId: number) {
  return orvalFetcher<{ revoked: boolean }>(`/api/hwid/devices/${deviceId}/revoke`, { method: 'POST' })
}

export function resetHwidUserDevices(userId: number) {
  return orvalFetcher<{ count: number }>(`/api/hwid/users/${userId}/reset`, { method: 'POST' })
}
