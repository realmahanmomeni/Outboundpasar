import { useTranslation } from 'react-i18next'
import { Card, CardContent, CardFooter, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Layers,
  Percent,
  RefreshCw,
  Server,
  Settings2,
  User,
  ExternalLink,
} from 'lucide-react'
import type { PanelItem } from '../service/panels-api'

interface PanelCardProps {
  panel: PanelItem
  onManage: (panel: PanelItem) => void
  onUpdate: (panel: PanelItem) => void
}

export default function PanelCard({ panel, onManage, onUpdate }: PanelCardProps) {
  const { t } = useTranslation()

  const getStatusBadge = () => {
    const raw = (panel.sync_status || '').toLowerCase().trim()
    switch (raw) {
      case 'connected':
        return (
          <Badge variant="green" className="gap-1.5 font-medium">
            <span className="h-1.5 w-1.5 rounded-full bg-emerald-500 animate-pulse" />
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
    <Card className="flex flex-col justify-between transition-all duration-200 hover:border-primary/40 hover:shadow-md">
      <div>
        <CardHeader className="pb-3">
          <div className="flex items-start justify-between gap-2">
            <div className="flex items-center gap-2.5 min-w-0">
              <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
                <Server className="h-5 w-5" />
              </div>
              <div className="min-w-0">
                <CardTitle className="text-base font-semibold truncate" title={panel.name}>
                  {panel.name}
                </CardTitle>
                <div className="flex items-center gap-1.5 text-xs text-muted-foreground mt-0.5">
                  <User className="h-3 w-3 shrink-0" />
                  <span className="font-medium text-foreground truncate">{panel.purchaser_identity}</span>
                  <span className="text-muted-foreground/40">•</span>
                  <span className="font-mono text-[11px] text-muted-foreground">#{panel.id}</span>
                </div>
              </div>
            </div>
            {getStatusBadge()}
          </div>
        </CardHeader>

        <CardContent className="space-y-2 pb-4 text-sm">
          <div className="grid grid-cols-2 gap-2 rounded-lg bg-muted/40 p-2.5 text-xs">
            <div className="flex items-center gap-2">
              <Layers className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
              <div className="min-w-0">
                <span className="text-muted-foreground block text-[11px]">{t('panels.configs', { defaultValue: 'Configs' })}</span>
                <span className="font-medium text-foreground">{panel.configs_count}</span>
              </div>
            </div>

            <div className="flex items-center gap-2">
              <Percent className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
              <div className="min-w-0">
                <span className="text-muted-foreground block text-[11px]">{t('panels.multiplier', { defaultValue: 'Multiplier' })}</span>
                <span className="font-medium text-foreground">{panel.default_multiplier}x</span>
              </div>
            </div>
          </div>

          <div className="flex items-center justify-between text-[11px] text-muted-foreground px-0.5 pt-1">
            <span>{t('panels.sourcePanelId', { defaultValue: 'Source' })}: {panel.source_panel_id}</span>
            {panel.last_sync_at && (
              <span>
                {t('panels.lastSync', { defaultValue: 'Synced' })}: {new Date(panel.last_sync_at).toLocaleDateString()}
              </span>
            )}
          </div>
        </CardContent>
      </div>

      <CardFooter className="flex items-center justify-end gap-2 pt-0 border-t bg-muted/20 py-2.5">
        <Button
          variant="outline"
          size="sm"
          className="h-8 gap-1.5 text-xs"
          onClick={() => onUpdate(panel)}
        >
          <RefreshCw className="h-3.5 w-3.5 text-muted-foreground" />
          <span>{t('panels.update', { defaultValue: 'Update' })}</span>
        </Button>
        <Button
          variant="default"
          size="sm"
          className="h-8 gap-1.5 text-xs"
          onClick={() => onManage(panel)}
        >
          <Settings2 className="h-3.5 w-3.5" />
          <span>{t('panels.manage', { defaultValue: 'Manage' })}</span>
        </Button>
      </CardFooter>
    </Card>
  )
}
