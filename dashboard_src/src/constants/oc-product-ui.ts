/**
 * PasarGuard 4.x product UI: OC / panel-focused dashboard.
 * Legacy native infrastructure pages remain in the codebase but are not user-facing.
 */
import type { AdminDetails } from '@/service/api'
import { isPlatformOwner } from '@/utils/admin-session'

export const LEGACY_NATIVE_INFRA_UI_ENABLED = false

export function showLegacyNativeInfraInNav(): boolean {
  return LEGACY_NATIVE_INFRA_UI_ENABLED
}

export function isLegacyNativeInfraRoute(pathname: string): boolean {
  if (LEGACY_NATIVE_INFRA_UI_ENABLED) return false
  if (pathname === '/nodes' || pathname === '/nodes/') return true
  if (pathname === '/nodes/native' || pathname.startsWith('/nodes/native/')) return true
  if (pathname.startsWith('/nodes/cores')) return true
  if (pathname.startsWith('/nodes/wireguard')) return true
  if (/^\/nodes\/\d+(\/|$)/.test(pathname)) return true
  return false
}

export function ocPanelFallbackRoute(): '/panels' {
  return '/panels'
}

/** Phase 2: hidden for every role (backend settings unchanged). */
const SETTINGS_HIDDEN_FOR_ALL_TAB_IDS = new Set(['general', 'notifications', 'telegram'])

/** Phase 2: platform owner only in the dashboard UI. */
const SETTINGS_OWNER_ONLY_TAB_IDS = new Set(['hwid', 'webhook'])

const SETTINGS_HIDDEN_FOR_ALL_PATHS = ['/settings/general', '/settings/notifications', '/settings/telegram'] as const

const SETTINGS_OWNER_ONLY_PATHS = ['/settings/hwid', '/settings/webhook'] as const

export function isSettingsTabVisible(tabId: string, admin: AdminDetails | null | undefined): boolean {
  if (SETTINGS_HIDDEN_FOR_ALL_TAB_IDS.has(tabId)) return false
  if (SETTINGS_OWNER_ONLY_TAB_IDS.has(tabId)) return isPlatformOwner(admin)
  return true
}

function matchesSettingsPath(pathname: string, base: string): boolean {
  return pathname === base || pathname.startsWith(`${base}/`)
}

export function isHiddenSettingsRoute(pathname: string, admin: AdminDetails | null | undefined): boolean {
  if (SETTINGS_HIDDEN_FOR_ALL_PATHS.some(path => matchesSettingsPath(pathname, path))) return true
  if (SETTINGS_OWNER_ONLY_PATHS.some(path => matchesSettingsPath(pathname, path))) {
    return !isPlatformOwner(admin)
  }
  return false
}

/** @deprecated Use isHiddenSettingsRoute(pathname, admin) */
export function hideAlertingSettingsInNav(): boolean {
  return true
}

/** Notifications + Telegram only (always hidden in OC product UI). */
export function isHiddenAlertingSettingsRoute(pathname: string): boolean {
  return SETTINGS_HIDDEN_FOR_ALL_PATHS.filter(p => p !== '/settings/general').some(path =>
    matchesSettingsPath(pathname, path),
  )
}

export function filterOcSettingsTabs<T extends { id: string }>(
  tabs: readonly T[],
  admin?: AdminDetails | null,
): T[] {
  return tabs.filter(tab => isSettingsTabVisible(tab.id, admin))
}

export function filterOcSettingsNavItems<T extends { url: string }>(
  items: readonly T[],
  admin?: AdminDetails | null,
): T[] {
  return items.filter(item => !isHiddenSettingsRoute(item.url, admin))
}

export function ocSettingsFallbackRoute(options: { canReadSettings: boolean }): string {
  if (options.canReadSettings) return '/settings/subscriptions'
  return '/settings/theme'
}

/** @deprecated Use ocSettingsFallbackRoute */
export function ocAlertingSettingsFallbackRoute(options: {
  canReadSettings: boolean
  canReadGeneral?: boolean
}): string {
  return ocSettingsFallbackRoute({ canReadSettings: options.canReadSettings })
}
