import { FetchError } from 'ofetch'
import { orvalFetcher } from './http'

export interface SubscriptionBrandingProfile {
  id: number
  workspace_id: number
  brand_name?: string | null
  logo_url?: string | null
  support_url?: string | null
  telegram_url?: string | null
  welcome_message?: string | null
  footer_text?: string | null
  primary_color?: string | null
  secondary_color?: string | null
  favicon_url?: string | null
  custom_css?: string | null
  support_text?: string | null
  customer_footer?: string | null
  hide_platform_branding?: boolean
  created_at: string
  updated_at: string
}

export type SubscriptionBrandingPayload = {
  brand_name?: string | null
  logo_url?: string | null
  support_url?: string | null
  telegram_url?: string | null
  welcome_message?: string | null
  footer_text?: string | null
  primary_color?: string | null
  secondary_color?: string | null
  favicon_url?: string | null
  custom_css?: string | null
  support_text?: string | null
  customer_footer?: string | null
  hide_platform_branding?: boolean
}

export type SubscriptionAppearancePreviewPayload = {
  use_active_template: boolean
  title?: string | null
  description?: string | null
  logo_url?: string | null
  footer_text?: string | null
  theme_primary?: string | null
}

const base = '/api/subscription/branding'

export async function getSubscriptionBranding(): Promise<SubscriptionBrandingProfile | null> {
  try {
    return await orvalFetcher<SubscriptionBrandingProfile>(base)
  } catch (error) {
    if (error instanceof FetchError && error.response?.status === 404) {
      return null
    }
    throw error
  }
}

export function createSubscriptionBranding(data: SubscriptionBrandingPayload) {
  return orvalFetcher<SubscriptionBrandingProfile>(base, {
    method: 'POST',
    body: JSON.stringify(data),
    headers: { 'Content-Type': 'application/json' },
  })
}

export function updateSubscriptionBranding(data: SubscriptionBrandingPayload) {
  return orvalFetcher<SubscriptionBrandingProfile>(base, {
    method: 'PUT',
    body: JSON.stringify(data),
    headers: { 'Content-Type': 'application/json' },
  })
}

export function deleteSubscriptionBranding() {
  return orvalFetcher<void>(base, { method: 'DELETE' })
}

export function previewSubscriptionCustomerPage(data: SubscriptionAppearancePreviewPayload) {
  return orvalFetcher<{ html: string }>(`${base}/preview`, {
    method: 'POST',
    body: JSON.stringify(data),
    headers: { 'Content-Type': 'application/json' },
  })
}
