import { fetcher } from '@/service/http'
import { useQuery } from '@tanstack/react-query'

export interface PanelItem {
  id: number
  integration_id: number
  name: string
  source_panel_id: string
  purchaser_identity: string
  sync_status: string | null
  default_multiplier: number
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
}

export const getAvailablePanels = () => fetcher<{ items: OCPanelItem[] }>('/api/integration/available-panels')

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

export interface PanelHostItem {
  id: number
  display_name: string
  source_config_name: string
  group_name: string | null
  address: string[]
  is_disabled: boolean
  multiplier: number | null
}

export interface PanelHostUpdateData {
  display_name?: string
  is_disabled?: boolean
  multiplier?: number
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
