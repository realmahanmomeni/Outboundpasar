import { useParams, useNavigate } from 'react-router'
import { useTranslation } from 'react-i18next'
import {
  AlertCircle,
  ArrowLeft,
  Calendar,
  Layers,
  Percent,
  RefreshCw,
  Server,
  ShieldAlert,
  User,
  UserCheck,
  Monitor,
} from 'lucide-react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { useGetPanel } from '../service/panels-api'
import { PanelHostsList } from './panel-hosts-list'

export default function PanelManage() {
  const { id } = useParams()
  const { t } = useTranslation()
  const navigate = useNavigate()

  const { data: panel, isLoading, isError, error, refetch, isFetching } = useGetPanel(id || '')

  const getStatusBadge = (status: string | null) => {
    const raw = (status || '').toLowerCase().trim()
    switch (raw) {
      case 'connected':
        return (
          <Badge variant="green" className="gap-1.5 font-medium">
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
            {t('panels.statusConnected', { defaultValue: 'Connected' })}
          </Badge>
        )
      case 'pending':
        return (
          <Badge variant="yellow" className="gap-1.5 font-medium">
            <span className="h-1.5 w-1.5 rounded-full bg-amber-500" />
            {t('panels.statusPending', { defaultValue: 'Pending' })}
          </Badge>
        )
      case 'error':
        return (
          <Badge variant="red" className="gap-1.5 font-medium">
            <span className="h-1.5 w-1.5 rounded-full bg-rose-500" />
            {t('panels.statusError', { defaultValue: 'Error' })}
          </Badge>
        )
      case 'disconnected':
        return (
          <Badge variant="secondary" className="gap-1.5 font-medium">
            <span className="h-1.5 w-1.5 rounded-full bg-muted-foreground/60" />
            {t('panels.statusDisconnected', { defaultValue: 'Disconnected' })}
          </Badge>
        )
      default:
        return (
          <Badge variant="outline" className="gap-1.5 text-muted-foreground">
            <span className="h-1.5 w-1.5 rounded-full bg-muted-foreground/40" />
            {t('panels.statusNotConnected', { defaultValue: 'Not connected' })}
          </Badge>
        )
    }
  }

  return (
    <div className="flex w-full flex-col p-4 sm:p-6 space-y-6">
      {/* Top back navigation */}
      <div className="flex items-center justify-between">
        <Button
          variant="ghost"
          size="sm"
          onClick={() => navigate('/panels')}
          className="gap-1.5 text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="h-4 w-4 rtl:rotate-180" />
          <span>{t('panels.title', { defaultValue: 'Panels' })}</span>
        </Button>

        <Button
          variant="outline"
          size="sm"
          onClick={() => refetch()}
          disabled={isFetching}
          className="h-8 gap-1.5 text-xs"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${isFetching ? 'animate-spin' : ''}`} />
          <span>{t('refresh', { defaultValue: 'Refresh' })}</span>
        </Button>
      </div>

      {/* Loading state */}
      {isLoading && (
        <div className="space-y-4">
          <Card className="p-6">
            <div className="space-y-3">
              <Skeleton className="h-7 w-48" />
              <Skeleton className="h-4 w-32" />
            </div>
          </Card>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <Card className="p-6 space-y-4">
              <Skeleton className="h-5 w-40" />
              <div className="space-y-2">
                <Skeleton className="h-4 w-full" />
                <Skeleton className="h-4 w-full" />
                <Skeleton className="h-4 w-full" />
              </div>
            </Card>
            <Card className="p-6 space-y-4">
              <Skeleton className="h-5 w-40" />
              <div className="space-y-2">
                <Skeleton className="h-4 w-full" />
                <Skeleton className="h-4 w-full" />
                <Skeleton className="h-4 w-full" />
              </div>
            </Card>
          </div>
        </div>
      )}

      {/* Error state */}
      {!isLoading && isError && (
        <Card className="border-destructive/30 bg-destructive/5 my-6">
          <CardContent className="flex flex-col items-center justify-center p-8 text-center space-y-3">
            <div className="flex h-12 w-12 items-center justify-center rounded-full bg-destructive/10 text-destructive">
              <AlertCircle className="h-6 w-6" />
            </div>
            <div className="space-y-1">
              <h3 className="text-base font-semibold text-foreground">
                Unable to load panel details
              </h3>
              <p className="text-sm text-muted-foreground max-w-sm">
                {(error as any)?.message || 'The requested panel could not be found or access is restricted.'}
              </p>
            </div>
            <Button
              variant="outline"
              size="sm"
              onClick={() => refetch()}
              className="mt-2 gap-1.5 border-destructive/30 hover:bg-destructive/10"
            >
              <RefreshCw className="h-3.5 w-3.5" />
              <span>{t('panels.retry', { defaultValue: 'Retry' })}</span>
            </Button>
          </CardContent>
        </Card>
      )}

      {/* Loaded Panel Shell */}
      {!isLoading && !isError && panel && (
        <div className="space-y-6">
          {/* Header Card */}
          <Card>
            <CardHeader className="pb-4">
              <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
                <div className="flex items-center gap-3">
                  <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-xl bg-primary/10 text-primary">
                    <Server className="h-6 w-6" />
                  </div>
                  <div>
                    <div className="flex items-center gap-2">
                      <CardTitle className="text-xl font-bold">{panel.name}</CardTitle>
                      <Badge variant="outline" className="font-mono text-xs">
                        #{panel.id}
                      </Badge>
                    </div>
                    <CardDescription className="flex items-center gap-2 text-xs mt-1">
                      <span>Purchaser: <strong className="text-foreground font-medium">{panel.purchaser_identity}</strong></span>
                      <span>•</span>
                      <span>Source ID: <strong className="text-foreground font-mono">{panel.source_panel_id}</strong></span>
                    </CardDescription>
                  </div>
                </div>
                <div>{getStatusBadge(panel.sync_status)}</div>
              </div>
            </CardHeader>
          </Card>

          {/* Details & Specifications Grid */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <Card>
              <CardHeader className="pb-3">
                <CardTitle className="text-sm font-semibold flex items-center gap-2">
                  <Layers className="h-4 w-4 text-primary" />
                  <span>Integration Status & Details</span>
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-3 text-sm">
                <div className="flex justify-between py-1 border-b text-xs">
                  <span className="text-muted-foreground">Purchased Instance ID</span>
                  <span className="font-mono font-medium">#{panel.id}</span>
                </div>
                <div className="flex justify-between py-1 border-b text-xs">
                  <span className="text-muted-foreground">Source Panel ID</span>
                  <span className="font-mono font-medium">{panel.source_panel_id}</span>
                </div>
                <div className="flex justify-between py-1 border-b text-xs">
                  <span className="text-muted-foreground">Purchaser Identity</span>
                  <span className="font-medium">{panel.purchaser_identity}</span>
                </div>
                <div className="flex justify-between py-1 border-b text-xs">
                  <span className="text-muted-foreground">Imported Configs Count</span>
                  <span className="font-medium">{panel.configs_count}</span>
                </div>
                <div className="flex justify-between py-1 border-b text-xs">
                  <span className="text-muted-foreground">Default Traffic Multiplier</span>
                  <span className="font-medium">{panel.default_multiplier}x</span>
                </div>
                <div className="flex justify-between py-1 border-b text-xs">
                  <span className="text-muted-foreground">Test User</span>
                  <span className="font-mono font-medium">{panel.test_user_id || 'None'}</span>
                </div>
                <div className="flex justify-between py-1 text-xs">
                  <span className="text-muted-foreground">Last Synchronization</span>
                  <span className="text-muted-foreground">
                    {panel.last_sync_at ? new Date(panel.last_sync_at).toLocaleString() : 'Never'}
                  </span>
                </div>
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="pb-3">
                <CardTitle className="text-sm font-semibold flex items-center gap-2">
                  <ShieldAlert className="h-4 w-4 text-amber-500" />
                  <span>Management Capabilities</span>
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-3 text-xs text-muted-foreground">
                <p>
                  This page establishes the foundation and shell for external panel management. Detailed integration actions will be enabled in subsequent phases:
                </p>

                <div className="rounded-lg border bg-muted/40 p-3 space-y-2 text-foreground/80">
                  <div className="flex items-center gap-2">
                    <span className="h-1.5 w-1.5 rounded-full bg-primary" />
                    <span><strong>Phase 5:</strong> Group discovery, inbound selection, host syncing</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="h-1.5 w-1.5 rounded-full bg-primary" />
                    <span><strong>Phase 6 & 7:</strong> User mapping and traffic synchronization</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="h-1.5 w-1.5 rounded-full bg-primary" />
                    <span><strong>Phase 8:</strong> Multiplier application & subscription links</span>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="h-1.5 w-1.5 rounded-full bg-primary" />
                    <span><strong>Phase 11:</strong> Active synchronization & state reconciliation</span>
                  </div>
                </div>

                <p className="text-[11px] text-muted-foreground/70">
                  No configuration synchronization or direct changes to external panels are performed in this phase.
                </p>
              </CardContent>
            </Card>
          </div>

          {/* Hosts Management */}
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-base font-semibold flex items-center gap-2">
                <Monitor className="h-5 w-5 text-primary" />
                <span>Imported Hosts</span>
              </CardTitle>
              <CardDescription>
                Manage local presentation and state of synchronized hosts.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <PanelHostsList panelId={panel.id} />
            </CardContent>
          </Card>
        </div>
      )}
    </div>
  )
}
