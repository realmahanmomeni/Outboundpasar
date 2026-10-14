import { Suspense } from 'react'
import { useAdmin } from '@/hooks/use-admin'
import { getCurrentAdmin } from '@/service/api'
import { unwrapAdminDetails } from '@/utils/admin-session'
import { hasPermission } from '@/utils/rbac'
import { createHashRouter, Navigate, RouteObject } from 'react-router'
import { LoadingSpinner } from '@/components/common/loading-spinner'
import { RouteErrorPage } from '@/components/layout/error-page'
import { TabbedRouteSuspenseFallback } from '@/components/layout/tabbed-route-suspense-fallback'
import { lazyWithChunkRecovery } from '@/utils/chunk-recovery'
import { ocPanelFallbackRoute, ocSettingsFallbackRoute, showLegacyNativeInfraInNav } from '@/constants/oc-product-ui'
import { isPlatformOwner } from '@/utils/admin-session'
import { isAuthenticationError } from '@/utils/error-utils'
// Replace direct imports with lazy imports for route-level components
const CoresLayout = lazyWithChunkRecovery(() => import('@/pages/_dashboard.nodes.cores'))
const CoresIndex = lazyWithChunkRecovery(() => import('@/pages/_dashboard.nodes.cores._index'))
const CoresCoreEditorRoute = lazyWithChunkRecovery(() => import('@/pages/_dashboard.nodes.cores.editor'))
const ThemePage = lazyWithChunkRecovery(() => import('@/pages/_dashboard.settings.theme'))
const DashboardLayout = lazyWithChunkRecovery(() => import('../pages/_dashboard'))
const Dashboard = lazyWithChunkRecovery(() => import('../pages/_dashboard._index'))
const AdminsPage = lazyWithChunkRecovery(() => import('../pages/_dashboard.admins'))
const AdminRolesPage = lazyWithChunkRecovery(() => import('../pages/_dashboard.admin-roles'))
const ApiKeysPage = lazyWithChunkRecovery(() => import('../pages/_dashboard.api-keys'))
const BulkPage = lazyWithChunkRecovery(() => import('../pages/_dashboard.bulk'))
const BulkCreatePage = lazyWithChunkRecovery(() => import('../pages/_dashboard.bulk.create'))
const BulkDataPage = lazyWithChunkRecovery(() => import('../pages/_dashboard.bulk.data'))
const BulkExpirePage = lazyWithChunkRecovery(() => import('../pages/_dashboard.bulk.expire'))
const BulkGroupsPage = lazyWithChunkRecovery(() => import('../pages/_dashboard.bulk.groups'))
const BulkProxyPage = lazyWithChunkRecovery(() => import('../pages/_dashboard.bulk.proxy'))
const Groups = lazyWithChunkRecovery(() => import('../pages/_dashboard.groups'))
const Hosts = lazyWithChunkRecovery(() => import('../pages/_dashboard.hosts'))
const Nodes = lazyWithChunkRecovery(() => import('../pages/_dashboard.nodes'))
const NodesPage = lazyWithChunkRecovery(() => import('../pages/_dashboard.nodes._index'))
const NodeManagePage = lazyWithChunkRecovery(() => import('../pages/_dashboard.nodes.$id'))
const PanelsPage = lazyWithChunkRecovery(() => import('../pages/_dashboard.panels'))
const PanelManagePage = lazyWithChunkRecovery(() => import('../pages/_dashboard.panels.$id'))
const NativeNodesPage = lazyWithChunkRecovery(() => import('../pages/_dashboard.nodes.native'))
const NodeLogs = lazyWithChunkRecovery(() => import('../pages/_dashboard.nodes.logs'))
const NodeWireGuard = lazyWithChunkRecovery(() => import('../pages/_dashboard.nodes.wireguard'))
const Settings = lazyWithChunkRecovery(() => import('../pages/_dashboard.settings'))
const CleanupSettings = lazyWithChunkRecovery(() => import('../pages/_dashboard.settings.cleanup'))
const HwidSettings = lazyWithChunkRecovery(() => import('../pages/_dashboard.settings.hwid'))
const NotificationSettings = lazyWithChunkRecovery(() => import('../pages/_dashboard.settings.notifications'))
const SubscriptionSettings = lazyWithChunkRecovery(() => import('../pages/_dashboard.settings.subscriptions'))
const SubscriptionAppearanceSettings = lazyWithChunkRecovery(
  () => import('../pages/_dashboard.settings.subscription-appearance'),
)
const TelegramSettings = lazyWithChunkRecovery(() => import('../pages/_dashboard.settings.telegram'))
const WebhookSettings = lazyWithChunkRecovery(() => import('../pages/_dashboard.settings.webhook'))
const Statistics = lazyWithChunkRecovery(() => import('../pages/_dashboard.statistics'))
const TemplatesLayout = lazyWithChunkRecovery(() => import('../pages/_dashboard.templates'))
const UserTemplates = lazyWithChunkRecovery(() => import('../pages/_dashboard.templates.user'))
const ClientTemplates = lazyWithChunkRecovery(() => import('../pages/_dashboard.templates.client'))
const Users = lazyWithChunkRecovery(() => import('../pages/_dashboard.users'))
const Login = lazyWithChunkRecovery(() => import('../pages/login'))

// Component to handle default settings routing based on user permissions
function SettingsIndex() {
  const { admin } = useAdmin()
  const canReadSettings = hasPermission(admin, 'settings', 'read')
  const defaultPath = ocSettingsFallbackRoute({ canReadSettings })

  return <Navigate to={defaultPath} replace />
}

function TemplatesIndex() {
  const { admin } = useAdmin()
  const defaultPath = hasPermission(admin, 'templates', 'read') ? '/templates/user' : hasPermission(admin, 'client_templates', 'read') ? '/templates/client' : '/settings/theme'

  return <Navigate to={defaultPath} replace />
}

const fetchAdminLoader = async (): Promise<import('@/service/api').AdminDetails> => {
  try {
    const response = await getCurrentAdmin()
    const admin = unwrapAdminDetails(response)
    if (!admin) {
      throw Response.redirect('/login')
    }
    return admin
  } catch (error) {
    if (isAuthenticationError(error)) {
      throw Response.redirect('/login')
    }

    throw error
  }
}

function OcPanelNavigate() {
  return <Navigate to={ocPanelFallbackRoute()} replace />
}

function SettingsVisibilityNavigate() {
  const { admin } = useAdmin()
  const canReadSettings = hasPermission(admin, 'settings', 'read')
  return <Navigate to={ocSettingsFallbackRoute({ canReadSettings })} replace />
}

function GloballyHiddenSettingsRoute() {
  return <SettingsVisibilityNavigate />
}

function AlertingSettingsRoute({ children }: { children: React.ReactNode }) {
  return <GloballyHiddenSettingsRoute />
}

function OwnerOnlySettingsRoute({ children }: { children: React.ReactNode }) {
  const { admin } = useAdmin()
  if (!isPlatformOwner(admin)) {
    return <SettingsVisibilityNavigate />
  }
  return <>{children}</>
}

function NodeDetailRoute() {
  if (!showLegacyNativeInfraInNav()) {
    return <OcPanelNavigate />
  }
  return (
    <Suspense fallback={<LoadingSpinner />}>
      <NodeManagePage />
    </Suspense>
  )
}

// Telegram Mini Apps append launch params after `#` (e.g. "#tgWebAppData=...&tgWebAppVersion=..."),
// which collides with createHashRouter reading window.location.hash as the route path and
// produces a "No route matches" 404 before React ever mounts. Reset it to a real route first.
// @telegram-apps/sdk's retrieveRawInitData() still recovers the original data afterward via the
// Navigation Timing API, which isn't affected by history.replaceState.
;(function sanitizeTelegramLaunchHash() {
  const { hash } = window.location
  if (hash && !hash.startsWith('#/') && hash.includes('tgWebApp')) {
    window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}#/`)
  }
})()

// Wrap all route elements in <Suspense fallback={<LoadingSpinner />}>
export const router = createHashRouter([
  {
    hydrateFallbackElement: <LoadingSpinner />,
    element: (
      <Suspense fallback={<LoadingSpinner />}>
        <DashboardLayout />
      </Suspense>
    ),
    errorElement: (
      <Suspense fallback={<LoadingSpinner />}>
        <RouteErrorPage />
      </Suspense>
    ),
    loader: fetchAdminLoader,
    children: [
      {
        path: '/',
        index: true,
        element: (
          <Suspense fallback={<LoadingSpinner />}>
            <Dashboard />
          </Suspense>
        ),
      },
      {
        path: '/users',
        element: (
          <Suspense fallback={<LoadingSpinner />}>
            <Users />
          </Suspense>
        ),
      },
      {
        path: '/statistics',
        element: (
          <Suspense fallback={<LoadingSpinner />}>
            <Statistics />
          </Suspense>
        ),
      },
      {
        path: '/hosts',
        element: (
          <Suspense fallback={<LoadingSpinner />}>
            <Hosts />
          </Suspense>
        ),
      },
      {
        path: '/nodes',
        element: (
          <Suspense fallback={<TabbedRouteSuspenseFallback />}>
            <Nodes />
          </Suspense>
        ),
        children: [
          {
            path: '/nodes',
            element: (
              <Suspense fallback={<LoadingSpinner />}>
                {showLegacyNativeInfraInNav() ? <NodesPage /> : <OcPanelNavigate />}
              </Suspense>
            ),
          },
          {
            path: '/nodes/cores',
            element: showLegacyNativeInfraInNav() ? (
              <Suspense fallback={<LoadingSpinner />}>
                <CoresLayout />
              </Suspense>
            ) : (
              <OcPanelNavigate />
            ),
            children: [
              {
                index: true,
                element: (
                  <Suspense fallback={<LoadingSpinner />}>
                    <CoresIndex />
                  </Suspense>
                ),
              },
              {
                path: ':coreId',
                element: (
                  <Suspense fallback={<LoadingSpinner />}>
                    <CoresCoreEditorRoute />
                  </Suspense>
                ),
              },
            ],
          },
          {
            path: '/nodes/logs',
            element: (
              <Suspense fallback={<LoadingSpinner />}>
                <NodeLogs />
              </Suspense>
            ),
          },
          {
            path: '/nodes/wireguard',
            element: showLegacyNativeInfraInNav() ? (
              <Suspense fallback={<LoadingSpinner />}>
                <NodeWireGuard />
              </Suspense>
            ) : (
              <OcPanelNavigate />
            ),
          },
          {
            path: '/nodes/:id',
            element: <NodeDetailRoute />,
          },
          {
            path: '/nodes/native',
            element: showLegacyNativeInfraInNav() ? (
              <Suspense fallback={<LoadingSpinner />}>
                <NativeNodesPage />
              </Suspense>
            ) : (
              <OcPanelNavigate />
            ),
          },
        ],
      },
      {
        path: '/panels',
        element: (
          <Suspense fallback={<LoadingSpinner />}>
            <PanelsPage />
          </Suspense>
        ),
      },
      {
        path: '/panels/:id',
        element: (
          <Suspense fallback={<LoadingSpinner />}>
            <PanelManagePage />
          </Suspense>
        ),
      },
      {
        path: '/groups',
        element: (
          <Suspense fallback={<LoadingSpinner />}>
            <Groups />
          </Suspense>
        ),
      },
      {
        path: '/templates',
        element: (
          <Suspense fallback={<TabbedRouteSuspenseFallback />}>
            <TemplatesLayout />
          </Suspense>
        ),
        children: [
          {
            index: true,
            element: <TemplatesIndex />,
          },
          {
            path: '/templates/user',
            element: (
              <Suspense fallback={<LoadingSpinner />}>
                <UserTemplates />
              </Suspense>
            ),
          },
          {
            path: '/templates/client',
            element: (
              <Suspense fallback={<LoadingSpinner />}>
                <ClientTemplates />
              </Suspense>
            ),
          },
        ],
      },
      {
        path: '/admins',
        element: (
          <Suspense fallback={<LoadingSpinner />}>
            <AdminsPage />
          </Suspense>
        ),
      },
      {
        path: '/admin-roles',
        element: (
          <Suspense fallback={<LoadingSpinner />}>
            <AdminRolesPage />
          </Suspense>
        ),
      },
      {
        path: '/api-keys',
        element: (
          <Suspense fallback={<LoadingSpinner />}>
            <ApiKeysPage />
          </Suspense>
        ),
      },
      {
        path: '/settings',
        element: (
          <Suspense fallback={<TabbedRouteSuspenseFallback />}>
            <Settings />
          </Suspense>
        ),
        children: [
          {
            path: '/settings',
            index: true,
            element: (
              <Suspense fallback={<LoadingSpinner />}>
                <SettingsIndex />
              </Suspense>
            ),
          },
          {
            path: '/settings/general',
            element: <GloballyHiddenSettingsRoute />,
          },
          {
            path: '/settings/notifications',
            element: (
              <AlertingSettingsRoute>
                <Suspense fallback={<LoadingSpinner />}>
                  <NotificationSettings />
                </Suspense>
              </AlertingSettingsRoute>
            ),
          },
          {
            path: '/settings/subscriptions',
            element: (
              <Suspense fallback={<LoadingSpinner />}>
                <SubscriptionSettings />
              </Suspense>
            ),
          },
          {
            path: '/settings/subscription-appearance',
            element: (
              <Suspense fallback={<LoadingSpinner />}>
                <SubscriptionAppearanceSettings />
              </Suspense>
            ),
          },
          {
            path: '/settings/hwid',
            element: (
              <OwnerOnlySettingsRoute>
                <Suspense fallback={<LoadingSpinner />}>
                  <HwidSettings />
                </Suspense>
              </OwnerOnlySettingsRoute>
            ),
          },
          {
            path: '/settings/telegram',
            element: (
              <AlertingSettingsRoute>
                <Suspense fallback={<LoadingSpinner />}>
                  <TelegramSettings />
                </Suspense>
              </AlertingSettingsRoute>
            ),
          },
          {
            path: '/settings/webhook',
            element: (
              <OwnerOnlySettingsRoute>
                <Suspense fallback={<LoadingSpinner />}>
                  <WebhookSettings />
                </Suspense>
              </OwnerOnlySettingsRoute>
            ),
          },
          {
            path: '/settings/cleanup',
            element: (
              <Suspense fallback={<LoadingSpinner />}>
                <CleanupSettings />
              </Suspense>
            ),
          },
          {
            path: '/settings/theme',
            element: (
              <Suspense fallback={<LoadingSpinner />}>
                <ThemePage />
              </Suspense>
            ),
          },
        ],
      },
      {
        path: '/bulk',
        element: (
          <Suspense fallback={<TabbedRouteSuspenseFallback />}>
            <BulkPage />
          </Suspense>
        ),
        children: [
          {
            path: '/bulk',
            index: true,
            element: (
              <Suspense fallback={<LoadingSpinner />}>
                <BulkCreatePage />
              </Suspense>
            ),
          },
          {
            path: '/bulk/groups',
            element: (
              <Suspense fallback={<LoadingSpinner />}>
                <BulkGroupsPage />
              </Suspense>
            ),
          },
          {
            path: '/bulk/create',
            element: (
              <Suspense fallback={<LoadingSpinner />}>
                <BulkCreatePage />
              </Suspense>
            ),
          },
          {
            path: '/bulk/proxy',
            element: (
              <Suspense fallback={<LoadingSpinner />}>
                <BulkProxyPage />
              </Suspense>
            ),
          },
          {
            path: '/bulk/expire',
            element: (
              <Suspense fallback={<LoadingSpinner />}>
                <BulkExpirePage />
              </Suspense>
            ),
          },
          {
            path: '/bulk/data',
            element: (
              <Suspense fallback={<LoadingSpinner />}>
                <BulkDataPage />
              </Suspense>
            ),
          },
        ],
      },
      {
        path: 'theme',
        element: (
          <Suspense fallback={<LoadingSpinner />}>
            <ThemePage />
          </Suspense>
        ),
      },
    ],
  },
  {
    path: '/login',
    hydrateFallbackElement: <LoadingSpinner />,
    element: (
      <Suspense fallback={<LoadingSpinner />}>
        <Login />
      </Suspense>
    ),
    errorElement: (
      <Suspense fallback={<LoadingSpinner />}>
        <RouteErrorPage />
      </Suspense>
    ),
  },
] as RouteObject[])
