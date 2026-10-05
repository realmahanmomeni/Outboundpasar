import { fetcher } from '@/service/http'
import { useQuery } from '@tanstack/react-query'

export interface PanelItem {
  id: number
  integration_id: number
  name: string
  source_panel_id: string
  purchaser_identity: string
  sync_status: string | null
  multiplier: number
  configs_count: number
  test_user_id: string | null
  last_sync_at: string | null
  created_at: string | null
  updated_at: string | null
}

export const getPanels = (): Promise<PanelItem[]> => {
  return fetcher<PanelItem[]>('/api/panels')
}

export const getPanel = (id: number | string): Promise<PanelItem> => {
  return fetcher<PanelItem>(`/api/panels/${id}`)
}

export const updatePanel = (id: number | string, data: { multiplier?: number; name?: string }): Promise<PanelItem> => {
  return fetcher<PanelItem>(`/api/panels/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(data)
  })
}

export const deletePanel = (id: number | string): Promise<void> => {
  return fetcher<void>(`/api/panels/${id}`, {
    method: 'DELETE',
  })
}

export const useGetPanels = () => {
  return useQuery<PanelItem[]>({
    queryKey: ['/api/panels'],
    queryFn: getPanels,
  })
}

export const useGetPanel = (id: number | string) => {
  return useQuery<PanelItem>({
    queryKey: ['/api/panels', String(id)],
    queryFn: () => getPanel(id),
    enabled: Boolean(id),
  })
}

export interface OCPanelItem {
  id: number
  panel_type: string
  name: string
  status: string
  already_imported?: boolean
  imported_panel_id?: number | null
}

export interface TelegramConnectionStatus {
  status: string
  active?: boolean
  telegram_user_id?: number
  oc_account_id?: number
  verified_at?: string
  connected_at?: string
  bot_url?: string | null
  step?: string | null
}

export function formatApiError(err: unknown, fallback: string): string {
  const anyErr = err as {
    data?: { detail?: unknown }
    response?: { _data?: { detail?: unknown }; data?: { detail?: unknown } }
    message?: string
  }
  const detail =
    anyErr?.data?.detail ??
    anyErr?.response?._data?.detail ??
    anyErr?.response?.data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  if (Array.isArray(detail) && detail.length > 0) return String(detail[0])
  if (anyErr?.message && !anyErr.message.startsWith('[')) return anyErr.message
  return fallback
}

export const getTelegramConnection = () =>
  fetcher<TelegramConnectionStatus>('/api/integration/telegram-connection')

export const startTelegramConnection = () =>
  fetcher<{ intent_id: string; bot_url: string; status: string }>('/api/integration/telegram-connection/start', {
    method: 'POST',
    body: JSON.stringify({}),
  })

export const confirmTelegramConnection = (code: string) =>
  fetcher<{ status: string; telegram_user_id?: number; oc_account_id?: number }>(
    '/api/integration/telegram-connection/confirm',
    { method: 'POST', body: JSON.stringify({ code }) }
  )

export const revokeTelegramConnection = () =>
  fetcher<void>('/api/integration/telegram-connection/revoke', {
    method: 'POST',
    body: JSON.stringify({}),
  })

export const getAvailablePanels = () => fetcher<{ items: OCPanelItem[] }>('/api/integration/available-panels')

export const importPanels = (subscription_ids: number[]) =>
  fetcher<{ imported: { subscription_id: number; panel_id: number; created: boolean }[] }>(
    '/api/integration/import-panels',
    { method: 'POST', body: JSON.stringify({ subscription_ids }) }
  )

export const selectPanel = (data: { source_panel_id: string; name: string }) => 
  fetcher<{ panel_id: number; source_panel_id: string; test_user_id: string | null }>('/api/integration/select-panel', {
    method: 'POST',
    body: JSON.stringify(data)
  })

export const createTestUser = (panelId: number) => 
  fetcher<{ test_user_id: string }>(`/api/integration/panels/${panelId}/test-user`, { method: 'POST' })

export const getGroups = (panelId: number) => 
  fetcher<{ groups: { id: string; name: string }[] }>(`/api/integration/panels/${panelId}/groups`)

export const syncPanel = (panelId: number, data: { selected_group_ids: string[]; group_names: Record<string, string> }) =>
  fetcher<{ status: string; configs_created: number; hosts_created: number }>(`/api/integration/panels/${panelId}/sync`, {
    method: 'POST',
    body: JSON.stringify(data)
  })

export interface OcHostTechnicalSummary {
  protocol?: string | null
  network?: string | null
  address: string[]
  port?: number | null
  subscription_synced?: boolean
}

export interface PanelHostItem {
  id: number
  display_name: string
  display_name_template?: string | null
  source_config_name: string
  group_name: string | null
  address: string[]
  technical?: OcHostTechnicalSummary | null
  is_disabled: boolean
  multiplier: number
  is_oc_imported?: boolean
}

export interface PanelHostUpdateData {
  display_name?: string
  display_name_template?: string
  is_disabled?: boolean
}

export const getPanelHosts = (panelId: number | string): Promise<PanelHostItem[]> => {
  return fetcher<PanelHostItem[]>(`/api/panels/${panelId}/hosts`)
}

export const updatePanelHost = (panelId: number | string, hostId: number, data: PanelHostUpdateData): Promise<PanelHostItem> => {
  return fetcher<PanelHostItem>(`/api/panels/${panelId}/hosts/${hostId}`, {
    method: 'PATCH',
    body: JSON.stringify(data)
  })
}

export const useGetPanelHosts = (panelId: number | string) => {
  return useQuery<PanelHostItem[]>({
    queryKey: ['/api/panels', String(panelId), 'hosts'],
    queryFn: () => getPanelHosts(panelId),
    enabled: Boolean(panelId),
  })
}
