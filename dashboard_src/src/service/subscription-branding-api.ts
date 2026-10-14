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
}

const base = '/api/subscription/branding'

export function getSubscriptionBranding() {
  return orvalFetcher<SubscriptionBrandingProfile>(base)
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
