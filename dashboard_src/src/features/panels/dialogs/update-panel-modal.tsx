import { useTranslation } from 'react-i18next'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { AlertCircle, RefreshCw, Server, ShieldAlert } from 'lucide-react'
import type { PanelItem } from '../service/panels-api'

interface UpdatePanelModalProps {
  panel: PanelItem | null
  isOpen: boolean
  onOpenChange: (open: boolean) => void
}

export default function UpdatePanelModal({
  panel,
  isOpen,
  onOpenChange,
}: UpdatePanelModalProps) {
  const { t } = useTranslation()

  if (!panel) return null

  return (
    <Dialog open={isOpen} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <div className="flex items-center gap-2">
            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10 text-primary">
              <RefreshCw className="h-5 w-5" />
            </div>
            <div>
              <DialogTitle>
                {t('panels.updatePlaceholderTitle', { defaultValue: 'Panel Update' })}: {panel.name}
              </DialogTitle>
              <DialogDescription className="text-xs">
                {panel.purchaser_identity} (Instance #{panel.id})
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>

        <div className="space-y-4 py-2 text-sm">
          <div className="rounded-lg border bg-muted/30 p-3 space-y-2">
            <div className="flex justify-between text-xs">
              <span className="text-muted-foreground">{t('panels.sourcePanelId', { defaultValue: 'Source Panel ID' })}:</span>
              <span className="font-mono font-medium">{panel.source_panel_id}</span>
            </div>
            <div className="flex justify-between text-xs">
              <span className="text-muted-foreground">{t('panels.status', { defaultValue: 'Status' })}:</span>
              <span className="font-medium capitalize">{panel.sync_status || 'Not connected'}</span>
            </div>
            <div className="flex justify-between text-xs">
              <span className="text-muted-foreground">{t('panels.configs', { defaultValue: 'Configs' })}:</span>
              <span className="font-medium">{panel.configs_count}</span>
            </div>
            <div className="flex justify-between text-xs">
              <span className="text-muted-foreground">{t('panels.multiplier', { defaultValue: 'Multiplier' })}:</span>
              <span className="font-medium">{panel.default_multiplier}x</span>
            </div>
          </div>

          <div className="flex items-start gap-2.5 rounded-md border border-amber-500/20 bg-amber-500/10 p-3 text-xs text-amber-700 dark:text-amber-400">
            <ShieldAlert className="h-4 w-4 shrink-0 mt-0.5" />
            <div className="space-y-1">
              <p className="font-semibold">
                {t('panels.updateNotice', {
                  defaultValue: 'Panel update synchronization will be available in Phase 11.',
                })}
              </p>
              <p className="text-muted-foreground">
                In Phase 4, no external configs are fetched and no hosts are modified. No fake success notifications will be emitted.
              </p>
            </div>
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
