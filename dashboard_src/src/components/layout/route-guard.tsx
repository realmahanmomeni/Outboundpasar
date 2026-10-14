import { useAdmin } from '@/hooks/use-admin'
import {
  isHiddenAlertingSettingsRoute,
  isLegacyNativeInfraRoute,
  ocAlertingSettingsFallbackRoute,
  ocPanelFallbackRoute,
} from '@/constants/oc-product-ui'
import { canAccessRoute, canReadResourcePage, firstAllowedRoute, hasPermission } from '@/utils/rbac'
import { useEffect, useRef } from 'react'
import { useLocation, useNavigate } from 'react-router'

export default function RouteGuard({ children }: { children: React.ReactNode }) {
  const { admin } = useAdmin()
  const location = useLocation()
  const navigate = useNavigate()
  const hasNavigatedRef = useRef(false)

  useEffect(() => {
    if (!admin) {
      hasNavigatedRef.current = false
      return // Wait for admin data to load
    }

    if (isLegacyNativeInfraRoute(location.pathname) && canReadResourcePage(admin, 'nodes')) {
      if (hasNavigatedRef.current) return
      hasNavigatedRef.current = true
      navigate(ocPanelFallbackRoute(), { replace: true })
      return
    }

    if (isHiddenAlertingSettingsRoute(location.pathname)) {
      if (hasNavigatedRef.current) return
      hasNavigatedRef.current = true
      const canUpdateSettings = hasPermission(admin, 'settings', 'update')
      const canReadSettings = hasPermission(admin, 'settings', 'read') && canUpdateSettings
      const canReadGeneral = hasPermission(admin, 'settings', 'read_general') && canUpdateSettings
      navigate(
        ocAlertingSettingsFallbackRoute({
          canReadSettings,
          canReadGeneral,
        }),
        { replace: true },
      )
      return
    }

    if (canAccessRoute(admin, location.pathname)) {
      hasNavigatedRef.current = false
      return
    }

    if (hasNavigatedRef.current) {
      return
    }

    hasNavigatedRef.current = true
    navigate(firstAllowedRoute(admin), { replace: true })
  }, [admin, location.pathname, navigate])

  // Reset navigation flag when pathname changes (after navigation completes)
  useEffect(() => {
    hasNavigatedRef.current = false
  }, [location.pathname])

  return <>{children}</>
}
