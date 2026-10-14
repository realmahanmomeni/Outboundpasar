import { getPanelStatisticsOverview } from '@/service/panel-statistics-api'
import { hasPermission } from '@/utils/rbac'
import { useQuery } from '@tanstack/react-query'
import { useAdmin } from './use-admin'

/** True when PANEL_STATISTICS_DASHBOARD_ENABLED is on (overview API not 503). */
export function usePanelStatisticsAvailable() {
  const { admin } = useAdmin()
  const canRead = hasPermission(admin, 'users', 'read')

  const overviewQuery = useQuery({
    queryKey: ['panel-statistics-overview'],
    queryFn: getPanelStatisticsOverview,
    enabled: canRead,
    retry: false,
    staleTime: 60_000,
  })

  const available = overviewQuery.isSuccess && overviewQuery.data != null

  return { available, overview: overviewQuery.data ?? null, overviewQuery }
}
