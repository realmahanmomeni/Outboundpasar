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
import { ArrowRight, CheckCircle2, Layers, Server, ShieldCheck, UserCheck, Wrench } from 'lucide-react'

interface AddPanelPlaceholderModalProps {
  isOpen: boolean
  onOpenChange: (open: boolean) => void
}

export default function AddPanelPlaceholderModal({
  isOpen,
  onOpenChange,
}: AddPanelPlaceholderModalProps) {
  const { t } = useTranslation()

  const steps = [
    { icon: Server, title: 'Select Purchased Panel', desc: 'Choose from external purchased instances' },
    { icon: UserCheck, title: 'Integration Test User', desc: 'Create or reuse dedicated test identity' },
    { icon: Layers, title: 'Fetch & Select Groups', desc: 'Discover inbounds and select target groups' },
    { icon: Wrench, title: 'Fetch Configs & Sync Hosts', desc: 'Map external protocols into local proxy hosts' },
  ]

  return (
    <Dialog open={isOpen} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <div className="flex items-center gap-2">
            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10 text-primary">
              <Server className="h-5 w-5" />
            </div>
            <div>
              <DialogTitle>{t('panels.addPanelPlaceholderTitle', { defaultValue: 'Add Purchased Panel' })}</DialogTitle>
              <DialogDescription className="text-xs">
                {t('panels.addNotice', { defaultValue: 'Phase 5 Integration Wizard' })}
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>

        <div className="space-y-4 py-2">
          <p className="text-muted-foreground text-sm">
            {t('panels.addPanelPlaceholderDescription', {
              defaultValue:
                'The full Add Panel integration wizard will be implemented in Phase 5. Below is the planned onboarding flow:',
            })}
          </p>

          <div className="space-y-2.5 rounded-lg border bg-muted/40 p-3">
            {steps.map((step, idx) => (
              <div key={idx} className="flex items-start gap-3 text-sm">
                <div className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-background border text-xs font-semibold text-primary">
                  {idx + 1}
                </div>
                <div className="flex-1">
                  <div className="font-medium text-foreground flex items-center gap-1.5">
                    <step.icon className="h-3.5 w-3.5 text-muted-foreground" />
                    <span>{step.title}</span>
                  </div>
                  <div className="text-xs text-muted-foreground">{step.desc}</div>
                </div>
              </div>
            ))}
          </div>

          <div className="flex items-center gap-2 rounded-md bg-blue-500/10 px-3 py-2 text-xs text-blue-600 dark:text-blue-400">
            <ShieldCheck className="h-4 w-4 shrink-0" />
            <span>Honest shell — no fake panel instances will be created in this phase.</span>
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
