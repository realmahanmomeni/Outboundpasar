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
