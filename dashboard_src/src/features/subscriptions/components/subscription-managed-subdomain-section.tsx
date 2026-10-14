import { Button } from '@/components/ui/button'
import { FormDescription, FormItem, FormLabel, FormMessage } from '@/components/ui/form'
import { Input } from '@/components/ui/input'
import { fetcher } from '@/service/http'
import { Globe } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

type ManagedSubdomainSettings = {
  domain_suffix: string
  label: string | null
  fqdn: string | null
  pending_claim_label: string | null
  state: string | null
  ownership_linked?: boolean
  serving_ready?: boolean
}

export function SubscriptionManagedSubdomainSection() {
  const { t } = useTranslation()
  const [suffix, setSuffix] = useState('outboundcenter.ir')
  const [label, setLabel] = useState('')
  const [pendingClaim, setPendingClaim] = useState<string | null>(null)
  const [activeFqdn, setActiveFqdn] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const data = await fetcher<ManagedSubdomainSettings>('/api/custom-domains/managed/settings')
      setSuffix(data.domain_suffix || 'outboundcenter.ir')
      setLabel(data.label || '')
      setPendingClaim(data.pending_claim_label)
      setActiveFqdn(data.fqdn)
    } catch {
      toast.error(t('settings.subscriptions.managedSubdomain.loadError', { defaultValue: 'Failed to load managed subdomain settings' }))
    } finally {
      setLoading(false)
    }
  }, [t])

  useEffect(() => {
    void load()
  }, [load])

  const save = async () => {
    setSaving(true)
    try {
      const data = await fetcher<ManagedSubdomainSettings>('/api/custom-domains/managed/settings', {
        method: 'PUT',
        body: { label: label.trim() ? label.trim() : null },
      })
      setLabel(data.label || '')
      setActiveFqdn(data.fqdn)
      setPendingClaim(data.pending_claim_label)
      toast.success(t('settings.subscriptions.managedSubdomain.saveSuccess', { defaultValue: 'Subscription subdomain updated' }))
    } catch (e: unknown) {
      const message = e && typeof e === 'object' && 'data' in e && (e as { data?: { detail?: string } }).data?.detail
      toast.error(typeof message === 'string' ? message : t('settings.subscriptions.managedSubdomain.saveFailed', { defaultValue: 'Failed to save subdomain' }))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="space-y-3 rounded-lg border p-4">
      <div className="space-y-1">
        <h3 className="flex items-center gap-2 text-base font-semibold sm:text-lg">
          <Globe className="h-4 w-4" />
          {t('settings.subscriptions.managedSubdomain.title', { defaultValue: 'Subscription subdomain' })}
        </h3>
        <p className="text-muted-foreground text-xs sm:text-sm">
          {t('settings.subscriptions.managedSubdomain.description', {
            defaultValue:
              'Register your subdomain in the Outbound Center Telegram bot (Connect to central panel), then enter the same name here.',
          })}
        </p>
      </div>

      {pendingClaim && !label && (
        <p className="text-sm text-amber-600 dark:text-amber-400">
          {t('settings.subscriptions.managedSubdomain.pendingClaim', {
            defaultValue: 'Bot registered {{label}} — enter it below to activate.',
            label: pendingClaim,
          })}
        </p>
      )}

      {activeFqdn && (
        <p className="text-muted-foreground text-xs font-mono sm:text-sm">
          {t('settings.subscriptions.managedSubdomain.active', { defaultValue: 'Serving on:' })} https://{activeFqdn}/…
        </p>
      )}
      {!activeFqdn && pendingClaim && (
        <p className="text-muted-foreground text-xs sm:text-sm">
          {t('settings.subscriptions.managedSubdomain.notServingYet', {
            defaultValue: 'Linked in Outbound Center; subscription links still use the default domain until public HTTPS ingress is ready.',
          })}
        </p>
      )}

      <FormItem className="space-y-2">
        <FormLabel className="text-xs font-medium sm:text-sm">
          {t('settings.subscriptions.managedSubdomain.label', { defaultValue: 'Subdomain label' })}
        </FormLabel>
        <div className="flex flex-wrap items-center gap-2">
          <Input
            value={label}
            onChange={e => setLabel(e.target.value.replace(/\s/g, '').toLowerCase())}
            disabled={loading || saving}
            placeholder={pendingClaim || 'alpha'}
            className="max-w-[12rem] font-mono text-xs sm:text-sm"
            aria-label={t('settings.subscriptions.managedSubdomain.label', { defaultValue: 'Subdomain label' })}
          />
          <span className="text-muted-foreground font-mono text-xs sm:text-sm">.{suffix}</span>
        </div>
        <FormDescription className="text-xs sm:text-sm">
          {t('settings.subscriptions.managedSubdomain.hint', {
            defaultValue: 'Leave empty to use the default subscription domain.',
          })}
        </FormDescription>
        <FormMessage />
      </FormItem>

      <Button type="button" size="sm" onClick={() => void save()} disabled={loading || saving}>
        {saving
          ? t('settings.subscriptions.managedSubdomain.saving', { defaultValue: 'Saving…' })
          : t('settings.subscriptions.managedSubdomain.save', { defaultValue: 'Save subdomain' })}
      </Button>
    </div>
  )
}
