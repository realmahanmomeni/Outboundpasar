import { fetcher } from '@/service/http'
import { useQuery } from '@tanstack/react-query'

export interface GroupHostOption {
  inbound_tag: string
  display_name: string
  host_id?: number | null
  destination_config_id?: string | null
  panel_id?: number | null
  is_oc_destination?: boolean
}

export const getGroupHostOptions = (): Promise<GroupHostOption[]> => {
  return fetcher<GroupHostOption[]>('/api/group-host-options')
}

export const useGroupHostOptions = (enabled: boolean) => {
  return useQuery<GroupHostOption[]>({
    queryKey: ['/api/group-host-options'],
    queryFn: getGroupHostOptions,
    enabled,
  })
}
