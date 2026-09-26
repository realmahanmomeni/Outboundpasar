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
import { useState, useEffect } from 'react'
import { AlertCircle, RefreshCw, Server, ShieldAlert, Loader2 } from 'lucide-react'
import type { PanelItem } from '../service/panels-api'
import { updatePanel } from '../service/panels-api'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { toast } from 'sonner'
import { useQueryClient } from '@tanstack/react-query'

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
  const queryClient = useQueryClient()
  const [multiplier, setMultiplier] = useState<string>('')
  const [isSubmitting, setIsSubmitting] = useState(false)
  
  useEffect(() => {
    if (panel && isOpen) {
      setMultiplier(panel.multiplier.toString())
    }
  }, [panel, isOpen])

  if (!panel) return null

  const handleUpdate = async () => {
    try {
      setIsSubmitting(true)
      const num = parseFloat(multiplier)
      if (isNaN(num) || num <= 0) {
         toast.error("Multiplier must be a positive number")
         setIsSubmitting(false)
         return
      }
      if (multiplier.includes('.') && multiplier.split('.')[1].length > 2) {
         toast.error("Multiplier cannot have more than 2 decimal places")
         setIsSubmitting(false)
         return
      }

      await updatePanel(panel.id, { multiplier: num })
      toast.success(t('panels.updateSuccess', { defaultValue: 'Panel updated successfully' }))
      queryClient.invalidateQueries({ queryKey: ['/api/panels'] })
      queryClient.invalidateQueries({ queryKey: ['/api/panels', String(panel.id)] })
      onOpenChange(false)
    } catch (e: any) {
      toast.error(e.message || "Failed to update panel")
    } finally {
      setIsSubmitting(false)
    }
  }

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
              <span className="text-muted-foreground">{t('panels.multiplier', { defaultValue: 'Current Multiplier' })}:</span>
              <span className="font-medium">{panel.multiplier}x</span>
            </div>
          </div>

          <div className="space-y-2 pt-2 border-t">
            <Label className="text-xs">{t('panels.purchasedPanelMultiplier', { defaultValue: 'Purchased Panel Multiplier' })}</Label>
            <Input 
              value={multiplier}
              onChange={(e) => setMultiplier(e.target.value)}
              placeholder="1.0"
              type="number"
              step="0.01"
              min="0.01"
              disabled={isSubmitting}
            />
            <p className="text-[10px] text-muted-foreground">
              Must be greater than 0, up to 2 decimal places. Affects traffic accounting for all hosts.
            </p>
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={isSubmitting}>
            Cancel
          </Button>
          <Button onClick={handleUpdate} disabled={isSubmitting}>
            {isSubmitting && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            Save
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
