import { useAdmin } from '@/hooks/use-admin'
import { isLegacyNativeInfraRoute, ocPanelFallbackRoute } from '@/constants/oc-product-ui'
import { canAccessRoute, canReadResourcePage, firstAllowedRoute } from '@/utils/rbac'
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
