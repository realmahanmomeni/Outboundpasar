/**
 * PasarGuard 4.x product UI: OC / panel-focused dashboard.
 * Legacy native infrastructure pages remain in the codebase but are not user-facing.
 */
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

/** Outbound Center: hide PasarGuard notification / admin-bot Telegram / webhook settings UI. */
export const OC_HIDE_ALERTING_SETTINGS_UI = true

const OC_HIDDEN_ALERTING_SETTINGS_TAB_IDS = new Set(['notifications', 'telegram', 'webhook'])

const OC_HIDDEN_ALERTING_SETTINGS_PATHS = ['/settings/notifications', '/settings/telegram', '/settings/webhook'] as const

export function hideAlertingSettingsInNav(): boolean {
  return OC_HIDE_ALERTING_SETTINGS_UI
}

export function isHiddenAlertingSettingsRoute(pathname: string): boolean {
  if (!OC_HIDE_ALERTING_SETTINGS_UI) return false
  return OC_HIDDEN_ALERTING_SETTINGS_PATHS.some(path => pathname === path || pathname.startsWith(`${path}/`))
}

export function filterOcSettingsTabs<T extends { id: string }>(tabs: readonly T[]): T[] {
  if (!OC_HIDE_ALERTING_SETTINGS_UI) return [...tabs]
  return tabs.filter(tab => !OC_HIDDEN_ALERTING_SETTINGS_TAB_IDS.has(tab.id))
}

export function filterOcSettingsNavItems<T extends { url: string }>(items: readonly T[]): T[] {
  if (!OC_HIDE_ALERTING_SETTINGS_UI) return [...items]
  return items.filter(item => !isHiddenAlertingSettingsRoute(item.url))
}

export function ocAlertingSettingsFallbackRoute(options: { canReadSettings: boolean; canReadGeneral: boolean }): string {
  if (options.canReadSettings) return '/settings/subscriptions'
  if (options.canReadGeneral) return '/settings/general'
  return '/settings/theme'
}
