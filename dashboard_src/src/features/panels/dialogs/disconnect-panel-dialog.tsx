import { useTranslation } from 'react-i18next'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Loader2 } from 'lucide-react'
import type { PanelItem } from '../service/panels-api'

interface DisconnectPanelDialogProps {
  panel: PanelItem | null
  isOpen: boolean
  isPending: boolean
  onOpenChange: (open: boolean) => void
  onConfirm: () => void
}

export default function DisconnectPanelDialog({
  panel,
  isOpen,
  isPending,
  onOpenChange,
  onConfirm,
}: DisconnectPanelDialogProps) {
  const { t } = useTranslation()

  if (!panel) return null

  return (
    <AlertDialog open={isOpen} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>
            {t('panels.disconnectTitle', { defaultValue: 'Disconnect panel?' })}
          </AlertDialogTitle>
          <AlertDialogDescription className="space-y-2 text-left">
            <span className="block">
              {t('panels.disconnectIntro', {
                defaultValue:
                  'This removes the panel import from PasarGuard for your tenant. Your Outbound Center server and subscription are not deleted.',
              })}
            </span>
            <span className="block font-medium text-foreground">
              {panel.name} (#{panel.id})
            </span>
            <span className="block text-xs">
              {t('panels.disconnectEffects', {
                defaultValue:
                  'Local hosts, configs, and user mappings for this panel will be cleaned up. Active users will receive delete sync jobs on the external panel where applicable.',
              })}
            </span>
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel disabled={isPending}>{t('cancel', { defaultValue: 'Cancel' })}</AlertDialogCancel>
          <AlertDialogAction variant="destructive" onClick={onConfirm} disabled={isPending}>
            {isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            {t('panels.disconnectConfirm', { defaultValue: 'Disconnect' })}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
