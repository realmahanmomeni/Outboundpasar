import { orvalFetcher } from './http'

export interface SubscriptionExperienceTheme {
  primary?: string
  background?: string
  foreground?: string
  card?: string
  accent?: string
}

export interface SubscriptionExperienceSupportLink {
  label: string
  url: string
}

export interface SubscriptionExperienceCustomBlock {
  title: string
  body: string
}

export interface SubscriptionExperienceTemplate {
  id: number
  name: string
  is_enabled: boolean
  is_active: boolean
  title?: string | null
  description?: string | null
  logo_url?: string | null
  theme?: SubscriptionExperienceTheme | null
  support_links: SubscriptionExperienceSupportLink[]
  footer_text?: string | null
  custom_blocks: SubscriptionExperienceCustomBlock[]
  created_at: string
}

export interface SubscriptionExperienceTemplateListResponse {
  templates: SubscriptionExperienceTemplate[]
}

export type SubscriptionExperienceTemplatePayload = {
  name: string
  is_enabled?: boolean
  title?: string | null
  description?: string | null
  logo_url?: string | null
  theme?: SubscriptionExperienceTheme | null
  support_links?: SubscriptionExperienceSupportLink[]
  footer_text?: string | null
  custom_blocks?: SubscriptionExperienceCustomBlock[]
}

const base = '/api/subscription/templates'

export async function listSubscriptionExperienceTemplates() {
  return orvalFetcher<SubscriptionExperienceTemplateListResponse>(base)
}

export async function createSubscriptionExperienceTemplate(data: SubscriptionExperienceTemplatePayload) {
  return orvalFetcher<SubscriptionExperienceTemplate>(base, {
    method: 'POST',
    body: JSON.stringify(data),
    headers: { 'Content-Type': 'application/json' },
  })
}

export async function updateSubscriptionExperienceTemplate(
  id: number,
  data: Partial<SubscriptionExperienceTemplatePayload>,
) {
  return orvalFetcher<SubscriptionExperienceTemplate>(`${base}/${id}`, {
    method: 'PUT',
    body: JSON.stringify(data),
    headers: { 'Content-Type': 'application/json' },
  })
}

export async function deleteSubscriptionExperienceTemplate(id: number) {
  return orvalFetcher<void>(`${base}/${id}`, { method: 'DELETE' })
}

export async function activateSubscriptionExperienceTemplate(templateId: number | null) {
  return orvalFetcher<SubscriptionExperienceTemplateListResponse>(`${base}/activate`, {
    method: 'POST',
    body: JSON.stringify({ template_id: templateId }),
    headers: { 'Content-Type': 'application/json' },
  })
}

export async function previewSubscriptionExperienceTemplate(
  data: Omit<SubscriptionExperienceTemplatePayload, 'name'>,
) {
  return orvalFetcher<{ html: string }>(`${base}/preview`, {
    method: 'POST',
    body: JSON.stringify(data),
    headers: { 'Content-Type': 'application/json' },
  })
}
