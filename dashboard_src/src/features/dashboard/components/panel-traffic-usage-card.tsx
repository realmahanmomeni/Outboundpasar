import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { ChartContainer, ChartTooltip, ChartTooltipContent } from '@/components/ui/chart'
import { Skeleton } from '@/components/ui/skeleton'
import { usePanelStatisticsAvailable } from '@/hooks/use-panel-statistics-available'
import { getPanelStatisticsPanels, type PanelStatisticsItem } from '@/service/panel-statistics-api'
import { formatBytes } from '@/utils/formatByte'
import { useQuery } from '@tanstack/react-query'
import { useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { Bar, BarChart, CartesianGrid, XAxis, YAxis } from 'recharts'

const chartConfig = {
  traffic: {
    label: 'Traffic',
    color: 'hsl(var(--primary))',
  },
}

export default function PanelTrafficUsageCard() {
  const { t } = useTranslation()
  const { available, overview } = usePanelStatisticsAvailable()

  const panelsQuery = useQuery({
    queryKey: ['panel-statistics-panels'],
    queryFn: getPanelStatisticsPanels,
    enabled: available,
    retry: false,
    staleTime: 30_000,
  })

  const chartData = useMemo(() => {
    const panels = panelsQuery.data?.panels ?? []
    return panels.map((panel: PanelStatisticsItem) => ({
      name: panel.panel_name,
      traffic: panel.traffic_used,
      activeUsers: panel.active_users,
      totalUsers: panel.total_users,
    }))
  }, [panelsQuery.data])

  if (!available) {
    return null
  }

  if (panelsQuery.isLoading && !panelsQuery.data) {
    return (
      <Card>
        <CardHeader>
          <Skeleton className="h-5 w-40" />
          <Skeleton className="mt-2 h-4 w-full max-w-sm" />
        </CardHeader>
        <CardContent>
          <Skeleton className="h-[240px] w-full sm:h-[320px]" />
        </CardContent>
      </Card>
    )
  }

  const totalTraffic = overview?.traffic_used ?? chartData.reduce((sum, row) => sum + row.traffic, 0)

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t('statistics.panelStats.usageTitle', { defaultValue: 'Panel traffic usage' })}</CardTitle>
        <CardDescription>
          {t('statistics.panelStats.usageHint', {
            defaultValue: 'Traffic consumed by users on each imported panel in your workspace.',
          })}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="text-muted-foreground text-sm">
          {t('statistics.panelStats.totalTraffic', { defaultValue: 'Total traffic' })}:{' '}
          <span className="text-foreground font-semibold tabular-nums">{formatBytes(totalTraffic)}</span>
        </div>
        {chartData.length === 0 ? (
          <p className="text-muted-foreground text-sm">{t('statistics.panelStats.noPanels', 'No panels in this workspace.')}</p>
        ) : (
          <ChartContainer config={chartConfig} className="aspect-auto h-[min(320px,50vh)] w-full">
            <BarChart data={chartData} margin={{ top: 8, right: 8, left: 0, bottom: 48 }}>
              <CartesianGrid vertical={false} strokeDasharray="3 3" />
              <XAxis dataKey="name" tickLine={false} axisLine={false} angle={-25} textAnchor="end" height={64} interval={0} tick={{ fontSize: 10 }} />
              <YAxis tickLine={false} axisLine={false} width={48} tickFormatter={v => formatBytes(Number(v), 0, true).toString()} tick={{ fontSize: 10 }} />
              <ChartTooltip
                content={
                  <ChartTooltipContent
                    formatter={(value, _name, item) => {
                      const payload = item.payload as { activeUsers: number; totalUsers: number }
                      return (
                        <div className="flex flex-col gap-1">
                          <span>{formatBytes(Number(value))}</span>
                          <span className="text-muted-foreground text-xs">
                            {t('statistics.panelStats.usersShort', {
                              active: payload.activeUsers,
                              total: payload.totalUsers,
                              defaultValue: `${payload.activeUsers} / ${payload.totalUsers} users`,
                            })}
                          </span>
                        </div>
                      )
                    }}
                  />
                }
              />
              <Bar dataKey="traffic" fill="var(--color-traffic)" radius={4} />
            </BarChart>
          </ChartContainer>
        )}
      </CardContent>
    </Card>
  )
}
