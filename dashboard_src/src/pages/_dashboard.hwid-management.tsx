import HwidManagementPage from '@/features/hwid-management/components/hwid-management-page'
import { useAdmin } from '@/hooks/use-admin'
import { hasPermission } from '@/utils/rbac'
import { Navigate } from 'react-router'

export default function HwidManagementRoute() {
  const { admin } = useAdmin()
  if (!hasPermission(admin, 'hwids', 'read')) {
    return <Navigate to="/" replace />
  }
  return <HwidManagementPage />
}
