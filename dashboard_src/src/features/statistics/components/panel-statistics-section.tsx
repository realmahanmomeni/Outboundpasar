import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { formatBytes } from '@/utils/formatByte'
import { getPanelStatisticsOverview, getPanelStatisticsPanels, type PanelStatisticsItem } from '@/service/panel-statistics-api'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'

function OverviewCard({ label, value }: { label: string; value: string | number }) {
  return (
    <Card className="overflow-hidden">
      <CardContent className="flex flex-col gap-1 p-4">
        <span className="text-muted-foreground text-xs">{label}</span>
        <span className="text-2xl font-semibold tabular-nums">{value}</span>
      </CardContent>
    </Card>
  )
}

function PanelRow({ panel }: { panel: PanelStatisticsItem }) {
  const { t } = useTranslation()
  const dist = panel.expiration_distribution
  return (
    <tr className="border-b last:border-0">
      <td className="px-3 py-2 font-medium">{panel.panel_name}</td>
      <td className="px-3 py-2 tabular-nums">{panel.sync_status ?? '—'}</td>
      <td className="px-3 py-2 tabular-nums">{panel.active_users} / {panel.total_users}</td>
      <td className="px-3 py-2 tabular-nums">{formatBytes(panel.traffic_used)}</td>
      <td className="px-3 py-2 text-muted-foreground text-xs">
        {panel.last_activity_at ? new Date(panel.last_activity_at).toLocaleString() : '—'}
      </td>
      <td className="px-3 py-2 text-xs text-muted-foreground">
        {t('statistics.panelStats.expiredShort', { count: dist.expired, defaultValue: `exp: ${dist.expired}` })}
      </td>
    </tr>
  )
}

export default function PanelStatisticsSection() {
  const { t } = useTranslation()

  const overviewQuery = useQuery({
    queryKey: ['panel-statistics-overview'],
    queryFn: getPanelStatisticsOverview,
    retry: false,
    staleTime: 30_000,
  })

  const panelsQuery = useQuery({
    queryKey: ['panel-statistics-panels'],
    queryFn: getPanelStatisticsPanels,
    enabled: overviewQuery.data != null,
    retry: false,
    staleTime: 30_000,
  })

  const overview = overviewQuery.data
  if (overviewQuery.isError || overviewQuery.isLoading || overview == null) {
    return null
  }

  return (
    <div className="flex w-full flex-col gap-4 px-4 pb-6">
      <div>
        <h2 className="text-lg font-semibold">{t('statistics.panelStats.title', 'Panel & business overview')}</h2>
        <p className="text-muted-foreground text-sm">
          {t(
            'statistics.panelStats.subtitle',
            'Workspace-scoped metrics for your imported panels and their users.',
          )}
        </p>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
        <OverviewCard label={t('statistics.users', 'Total users')} value={overview.total_users} />
        <OverviewCard label={t('statistics.activeUsers', 'Active users')} value={overview.active_users} />
        <OverviewCard label={t('statistics.expiredUsers', 'Expired users')} value={overview.expired_users} />
        <OverviewCard label={t('statistics.onlineUsers', 'Online users')} value={overview.online_users} />
        <OverviewCard label={t('statistics.panelStats.panels', 'Active panels')} value={overview.panel_count} />
        <OverviewCard label={t('statistics.panelStats.traffic', 'Traffic used')} value={formatBytes(overview.traffic_used)} />
      </div>

      <Card>
        <CardHeader>
          <CardTitle>{t('statistics.panelStats.breakdown', 'Panel breakdown')}</CardTitle>
          <CardDescription>{t('statistics.panelStats.breakdownHint', 'Users and traffic per imported panel in your workspace.')}</CardDescription>
        </CardHeader>
        <CardContent className="overflow-x-auto p-0">
          {panelsQuery.isLoading ? null : (
            <table className="w-full min-w-[640px] text-sm">
              <thead>
                <tr className="border-b bg-muted/40 text-left text-xs text-muted-foreground">
                  <th className="px-3 py-2">{t('panels.name', 'Panel')}</th>
                  <th className="px-3 py-2">{t('common.status', 'Status')}</th>
                  <th className="px-3 py-2">{t('statistics.panelStats.users', 'Users (active/total)')}</th>
                  <th className="px-3 py-2">{t('statistics.panelStats.traffic', 'Traffic')}</th>
                  <th className="px-3 py-2">{t('statistics.panelStats.lastActivity', 'Last activity')}</th>
                  <th className="px-3 py-2">{t('statistics.panelStats.distribution', 'Expiry')}</th>
                </tr>
              </thead>
              <tbody>
                {(panelsQuery.data?.panels ?? []).length === 0 ? (
                  <tr>
                    <td colSpan={6} className="px-3 py-6 text-center text-muted-foreground">
                      {t('statistics.panelStats.noPanels', 'No panels in this workspace.')}
                    </td>
                  </tr>
                ) : (
                  panelsQuery.data?.panels.map((panel) => <PanelRow key={panel.panel_id} panel={panel} />)
                )}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
