import { FetchError } from 'ofetch'
import { orvalFetcher } from './http'

function isPanelStatisticsDisabledError(error: unknown): boolean {
  return error instanceof FetchError && error.response?.status === 503
}

export interface PanelStatisticsOverview {
  total_users: number
  active_users: number
  expired_users: number
  online_users: number
  active_subscriptions: number
  expired_subscriptions: number
  traffic_used: number
  panel_count: number
}

export interface PanelExpirationDistribution {
  active: number
  expired: number
  disabled: number
  limited: number
  on_hold: number
}

export interface PanelStatisticsItem {
  panel_id: number
  panel_name: string
  sync_status?: string | null
  total_users: number
  active_users: number
  traffic_used: number
  last_activity_at?: string | null
  expiration_distribution: PanelExpirationDistribution
}

export interface PanelStatisticsListResponse {
  panels: PanelStatisticsItem[]
}

const overviewUrl = '/api/statistics/overview'
const panelsUrl = '/api/statistics/panels'

/** Returns null when the panel statistics feature flag is off (HTTP 503). */
export async function getPanelStatisticsOverview(): Promise<PanelStatisticsOverview | null> {
  try {
    return await orvalFetcher<PanelStatisticsOverview>(overviewUrl)
  } catch (error) {
    if (isPanelStatisticsDisabledError(error)) {
      return null
    }
    throw error
  }
}

export async function getPanelStatisticsPanels(): Promise<PanelStatisticsListResponse | null> {
  try {
    return await orvalFetcher<PanelStatisticsListResponse>(panelsUrl)
  } catch (error) {
    if (isPanelStatisticsDisabledError(error)) {
      return null
    }
    throw error
  }
}
