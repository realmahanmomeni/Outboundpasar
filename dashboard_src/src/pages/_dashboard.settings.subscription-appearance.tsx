import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'
import { Textarea } from '@/components/ui/textarea'
import {
  activateSubscriptionExperienceTemplate,
  createSubscriptionExperienceTemplate,
  deleteSubscriptionExperienceTemplate,
  listSubscriptionExperienceTemplates,
  type SubscriptionExperienceTemplate,
  type SubscriptionExperienceTemplatePayload,
  updateSubscriptionExperienceTemplate,
} from '@/service/subscription-experience-api'
import {
  createSubscriptionBranding,
  deleteSubscriptionBranding,
  getSubscriptionBranding,
  previewSubscriptionCustomerPage,
  updateSubscriptionBranding,
  type SubscriptionBrandingPayload,
} from '@/service/subscription-branding-api'
import { useAdmin } from '@/hooks/use-admin'
import { isPlatformOwner } from '@/utils/admin-session'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

const emptyForm: SubscriptionExperienceTemplatePayload = {
  name: '',
  is_enabled: true,
  title: '',
  description: '',
  logo_url: '',
  theme: {},
  support_links: [],
  footer_text: '',
  custom_blocks: [],
}

const emptyBranding: SubscriptionBrandingPayload = {
  brand_name: '',
  logo_url: '',
  support_url: '',
  telegram_url: '',
  welcome_message: '',
  footer_text: '',
  primary_color: '',
  secondary_color: '',
  favicon_url: '',
  custom_css: '',
  support_text: '',
  customer_footer: '',
  hide_platform_branding: false,
}

export default function SubscriptionAppearanceSettings() {
  const { t } = useTranslation()
  const { admin } = useAdmin()
  const queryClient = useQueryClient()
  const [selectedId, setSelectedId] = useState<number | 'new' | null>(null)
  const [form, setForm] = useState<SubscriptionExperienceTemplatePayload>(emptyForm)
  const [previewHtml, setPreviewHtml] = useState<string>('')
  const [brandingForm, setBrandingForm] = useState<SubscriptionBrandingPayload>(emptyBranding)

  const canManageWorkspaceAppearance = Boolean(admin && !isPlatformOwner(admin))

  const { data: brandingData, refetch: refetchBranding, isLoading: brandingLoading } = useQuery({
    queryKey: ['subscription-branding'],
    enabled: canManageWorkspaceAppearance,
    retry: false,
    queryFn: () => getSubscriptionBranding(),
  })

  useEffect(() => {
    if (brandingData) {
      setBrandingForm({
        brand_name: brandingData.brand_name ?? '',
        logo_url: brandingData.logo_url ?? '',
        support_url: brandingData.support_url ?? '',
        telegram_url: brandingData.telegram_url ?? '',
        welcome_message: brandingData.welcome_message ?? '',
        footer_text: brandingData.footer_text ?? '',
        primary_color: brandingData.primary_color ?? '',
        secondary_color: brandingData.secondary_color ?? '',
        favicon_url: brandingData.favicon_url ?? '',
        custom_css: brandingData.custom_css ?? '',
        support_text: brandingData.support_text ?? '',
        customer_footer: brandingData.customer_footer ?? '',
        hide_platform_branding: brandingData.hide_platform_branding ?? false,
      })
    }
  }, [brandingData])

  const { data, isLoading, isError: templatesError } = useQuery({
    queryKey: ['subscription-experience-templates'],
    enabled: canManageWorkspaceAppearance,
    retry: false,
    queryFn: () => listSubscriptionExperienceTemplates(),
  })

  const templates = data?.templates ?? []

  useEffect(() => {
    if (selectedId === null && templates.length > 0) {
      setSelectedId(templates[0].id)
    }
  }, [templates, selectedId])

  useEffect(() => {
    if (selectedId === 'new') {
      setForm({ ...emptyForm, name: `template-${Date.now()}` })
      return
    }
    if (typeof selectedId === 'number') {
      const row = templates.find((tpl) => tpl.id === selectedId)
      if (row) {
        setForm({
          name: row.name,
          is_enabled: row.is_enabled,
          title: row.title ?? '',
          description: row.description ?? '',
          logo_url: row.logo_url ?? '',
          theme: row.theme ?? {},
          support_links: row.support_links ?? [],
          footer_text: row.footer_text ?? '',
          custom_blocks: row.custom_blocks ?? [],
        })
      }
    }
  }, [selectedId, templates])

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['subscription-experience-templates'] })

  const saveMutation = useMutation({
    mutationFn: async () => {
      if (selectedId === 'new') {
        return createSubscriptionExperienceTemplate(form)
      }
      if (typeof selectedId === 'number') {
        return updateSubscriptionExperienceTemplate(selectedId, form)
      }
      throw new Error('Nothing selected')
    },
    onSuccess: (row) => {
      toast.success(t('common.saved', 'Saved'))
      setSelectedId(row.id)
      invalidate()
    },
    onError: () => toast.error(t('common.error', 'Error')),
  })

  const deleteMutation = useMutation({
    mutationFn: (id: number) => deleteSubscriptionExperienceTemplate(id),
    onSuccess: () => {
      toast.success(t('common.deleted', 'Deleted'))
      setSelectedId(null)
      invalidate()
    },
  })

  const activateMutation = useMutation({
    mutationFn: (id: number | null) => activateSubscriptionExperienceTemplate(id),
    onSuccess: () => {
      toast.success(t('settings.subscriptionAppearance.activated', 'Template activated'))
      invalidate()
    },
  })

  const customerPreviewMutation = useMutation({
    mutationFn: () => {
      const editingTemplate = selectedId === 'new' || typeof selectedId === 'number'
      return previewSubscriptionCustomerPage({
        use_active_template: !editingTemplate,
        title: form.title,
        description: form.description,
        logo_url: form.logo_url,
        footer_text: form.footer_text,
        theme_primary: form.theme?.primary,
      }).then((res) => res.html)
    },
    onSuccess: (html) => setPreviewHtml(html),
    onError: () => toast.error(t('settings.subscriptionAppearance.previewFailed', 'Preview failed')),
  })

  const activeTemplate = templates.find((tpl) => tpl.is_active)

  const pageHeader = (
    <div>
      <h2 className="text-lg font-semibold">{t('settings.subscriptionAppearance.title', 'Subscription appearance')}</h2>
      <p className="text-sm text-muted-foreground">
        {t(
          'settings.subscriptionAppearance.description',
          'Customize the browser subscription page for your workspace. Subscription config/token APIs stay unchanged until server feature flags are enabled.',
        )}
      </p>
    </div>
  )

  if (!canManageWorkspaceAppearance) {
    return (
      <div className="space-y-6 p-1">
        {pageHeader}
        <Card>
          <CardContent className="p-6 text-sm text-muted-foreground">
            {t(
              'settings.subscriptionAppearance.platformOwnerHint',
              'Branding and experience templates apply per tenant workspace. Platform owners do not have a workspace row—sign in as a tenant administrator (for example your sales bot admin) to configure subscription appearance.',
            )}
          </CardContent>
        </Card>
      </div>
    )
  }

  return (
    <div className="space-y-6 p-1">
      {pageHeader}

      <Card>
        <CardHeader>
          <CardTitle>{t('settings.subscriptionBranding.title', 'White-label branding')}</CardTitle>
          <CardDescription>
            {t('settings.subscriptionBranding.hint', 'One profile per workspace. Merged under active template when flags are on.')}
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3 md:grid-cols-2">
          {brandingLoading ? (
            <p className="text-muted-foreground text-sm md:col-span-2">{t('common.loading', 'Loading…')}</p>
          ) : null}
          <div>
            <Label>{t('settings.subscriptionBranding.brandName', 'Brand name')}</Label>
            <Input value={brandingForm.brand_name ?? ''} onChange={(e) => setBrandingForm({ ...brandingForm, brand_name: e.target.value })} />
          </div>
          <div>
            <Label>{t('settings.subscriptionAppearance.logoUrl', 'Logo URL')}</Label>
            <Input value={brandingForm.logo_url ?? ''} onChange={(e) => setBrandingForm({ ...brandingForm, logo_url: e.target.value })} />
          </div>
          <div>
            <Label>{t('settings.subscriptionBranding.favicon', 'Favicon URL')}</Label>
            <Input value={brandingForm.favicon_url ?? ''} onChange={(e) => setBrandingForm({ ...brandingForm, favicon_url: e.target.value })} />
          </div>
          <div>
            <Label>{t('settings.subscriptions.support_url', 'Support URL')}</Label>
            <Input value={brandingForm.support_url ?? ''} onChange={(e) => setBrandingForm({ ...brandingForm, support_url: e.target.value })} />
          </div>
          <div>
            <Label>{t('settings.subscriptionBranding.telegram', 'Telegram URL')}</Label>
            <Input value={brandingForm.telegram_url ?? ''} onChange={(e) => setBrandingForm({ ...brandingForm, telegram_url: e.target.value })} />
          </div>
          <div>
            <Label>{t('settings.subscriptionAppearance.primaryColor', 'Primary color (HSL)')}</Label>
            <Input
              placeholder="216 46% 40%"
              value={brandingForm.primary_color ?? ''}
              onChange={(e) => setBrandingForm({ ...brandingForm, primary_color: e.target.value })}
            />
          </div>
          <div>
            <Label>{t('settings.subscriptionAppearance.accentColor', 'Accent color (HSL)')}</Label>
            <Input
              placeholder="240 5% 90%"
              value={brandingForm.secondary_color ?? ''}
              onChange={(e) => setBrandingForm({ ...brandingForm, secondary_color: e.target.value })}
            />
          </div>
          <div className="md:col-span-2">
            <Label>{t('settings.subscriptionBranding.welcome', 'Welcome message')}</Label>
            <Textarea value={brandingForm.welcome_message ?? ''} onChange={(e) => setBrandingForm({ ...brandingForm, welcome_message: e.target.value })} />
          </div>
          <div className="md:col-span-2">
            <Label>{t('settings.subscriptionBranding.supportText', 'Support text')}</Label>
            <Textarea value={brandingForm.support_text ?? ''} onChange={(e) => setBrandingForm({ ...brandingForm, support_text: e.target.value })} />
          </div>
          <div className="md:col-span-2">
            <Label>{t('settings.subscriptionBranding.customerFooter', 'Customer footer')}</Label>
            <Textarea value={brandingForm.customer_footer ?? ''} onChange={(e) => setBrandingForm({ ...brandingForm, customer_footer: e.target.value })} />
          </div>
          <div className="md:col-span-2">
            <Label>{t('settings.subscriptionBranding.customCss', 'Custom CSS')}</Label>
            <Textarea
              className="font-mono text-xs"
              rows={4}
              value={brandingForm.custom_css ?? ''}
              onChange={(e) => setBrandingForm({ ...brandingForm, custom_css: e.target.value })}
            />
          </div>
          <div className="flex items-center gap-2 md:col-span-2">
            <Switch
              checked={brandingForm.hide_platform_branding ?? false}
              onCheckedChange={(v) => setBrandingForm({ ...brandingForm, hide_platform_branding: v })}
            />
            <Label>{t('settings.subscriptionBranding.hidePlatform', 'Hide recommended applications block')}</Label>
          </div>
          <div className="md:col-span-2 flex flex-wrap gap-2">
            <Button
              type="button"
              onClick={async () => {
                try {
                  if (brandingData) {
                    await updateSubscriptionBranding(brandingForm)
                  } else {
                    await createSubscriptionBranding(brandingForm)
                  }
                  toast.success(t('common.saved', 'Saved'))
                  await refetchBranding()
                } catch {
                  toast.error(t('common.error', 'Error'))
                }
              }}
            >
              {t('common.save', 'Save branding')}
            </Button>
            {brandingData ? (
              <Button
                type="button"
                variant="destructive"
                onClick={async () => {
                  await deleteSubscriptionBranding()
                  toast.success(t('common.deleted', 'Deleted'))
                  await refetchBranding()
                }}
              >
                {t('common.delete', 'Delete branding')}
              </Button>
            ) : null}
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t('settings.subscriptionAppearance.templates', 'Experience templates')}</CardTitle>
          <CardDescription>
            {activeTemplate
              ? t('settings.subscriptionAppearance.active', { name: activeTemplate.name, defaultValue: `Active: ${activeTemplate.name}` })
              : t('settings.subscriptionAppearance.noActive', 'No active template (branding-only or default page)')}
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-wrap gap-2">
          {isLoading ? (
            <span className="text-sm text-muted-foreground">{t('common.loading', 'Loading…')}</span>
          ) : templatesError ? (
            <span className="text-sm text-muted-foreground">
              {t('settings.subscriptionAppearance.templatesUnavailable', 'Templates could not be loaded for this workspace.')}
            </span>
          ) : (
            templates.map((tpl: SubscriptionExperienceTemplate) => (
              <Button key={tpl.id} variant={selectedId === tpl.id ? 'default' : 'outline'} size="sm" onClick={() => setSelectedId(tpl.id)}>
                {tpl.name}
                {tpl.is_active ? ' ✓' : ''}
              </Button>
            ))
          )}
          <Button variant="secondary" size="sm" onClick={() => setSelectedId('new')}>
            {t('common.create', 'Create')}
          </Button>
        </CardContent>
      </Card>

      {(selectedId === 'new' || typeof selectedId === 'number') && (
        <Card>
          <CardHeader>
            <CardTitle>{t('settings.subscriptionAppearance.editor', 'Template editor')}</CardTitle>
          </CardHeader>
          <CardContent className="grid gap-4 md:grid-cols-2">
            <div className="space-y-3">
              <div>
                <Label>{t('common.name', 'Name')}</Label>
                <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
              </div>
              <div className="flex items-center gap-2">
                <Switch checked={form.is_enabled ?? true} onCheckedChange={(v) => setForm({ ...form, is_enabled: v })} />
                <Label>{t('common.enabled', 'Enabled')}</Label>
              </div>
              <div>
                <Label>{t('settings.subscriptions.profile_title', 'Title')}</Label>
                <Input value={form.title ?? ''} onChange={(e) => setForm({ ...form, title: e.target.value })} />
              </div>
              <div>
                <Label>{t('common.description', 'Description')}</Label>
                <Textarea value={form.description ?? ''} onChange={(e) => setForm({ ...form, description: e.target.value })} />
              </div>
              <div>
                <Label>{t('settings.subscriptionAppearance.logoUrl', 'Logo URL')}</Label>
                <Input value={form.logo_url ?? ''} onChange={(e) => setForm({ ...form, logo_url: e.target.value })} />
              </div>
              <div>
                <Label>{t('settings.subscriptionAppearance.primaryColor', 'Primary color (HSL components)')}</Label>
                <Input
                  placeholder="216 46% 40%"
                  value={form.theme?.primary ?? ''}
                  onChange={(e) => setForm({ ...form, theme: { ...form.theme, primary: e.target.value } })}
                />
              </div>
              <div>
                <Label>{t('settings.subscriptionAppearance.footer', 'Footer')}</Label>
                <Textarea value={form.footer_text ?? ''} onChange={(e) => setForm({ ...form, footer_text: e.target.value })} />
              </div>
            </div>
            <div className="flex flex-wrap gap-2 md:col-span-2">
              <Button onClick={() => saveMutation.mutate()} disabled={saveMutation.isPending}>
                {t('common.save', 'Save')}
              </Button>
              {typeof selectedId === 'number' && (
                <>
                  <Button variant="secondary" onClick={() => activateMutation.mutate(selectedId)} disabled={activateMutation.isPending}>
                    {t('settings.subscriptionAppearance.setActive', 'Set active')}
                  </Button>
                  <Button variant="ghost" onClick={() => activateMutation.mutate(null)}>
                    {t('settings.subscriptionAppearance.clearActive', 'Use default page')}
                  </Button>
                  <Button variant="destructive" onClick={() => deleteMutation.mutate(selectedId)}>
                    {t('common.delete', 'Delete')}
                  </Button>
                </>
              )}
            </div>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle>{t('settings.subscriptionAppearance.customerPreview', 'Customer page preview')}</CardTitle>
          <CardDescription>
            {t(
              'settings.subscriptionAppearance.customerPreviewHint',
              'Uses the same HTML template as live subscription pages, merging the active template (or current editor draft) with your workspace branding.',
            )}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <Button type="button" variant="outline" onClick={() => customerPreviewMutation.mutate()} disabled={customerPreviewMutation.isPending}>
            {t('settings.subscriptionAppearance.preview', 'Preview customer page')}
          </Button>
          {previewHtml ? (
            <iframe
              title="subscription-preview"
              className="h-[min(70vh,720px)] w-full rounded-md border bg-white"
              sandbox="allow-same-origin"
              srcDoc={previewHtml}
            />
          ) : null}
        </CardContent>
      </Card>
    </div>
  )
}
