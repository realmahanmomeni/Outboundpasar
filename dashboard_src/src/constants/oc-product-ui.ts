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
