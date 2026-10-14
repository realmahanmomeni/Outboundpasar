import { Footer } from '@/components/layout/footer'
import { AppSidebar } from '@/components/layout/sidebar'
import PageTransition from '@/components/layout/page-transition'
import RouteGuard from '@/components/layout/route-guard'
import { TopLoadingBar } from '@/components/layout/top-loading-bar'
import { VersionUpdateBanner } from '@/components/layout/version-update-banner'
import TopbarAd from '@/components/common/topbar-ad'
import { SidebarInset, SidebarProvider } from '@/components/ui/sidebar'
import { getCurrentAdmin } from '@/service/api'
import { unwrapAdminDetails } from '@/utils/admin-session'
import { isAuthenticationError } from '@/utils/error-utils'
import { Outlet } from 'react-router'
import { CommandPalette } from '@/components/layout/command-palette'

export const clientLoader = async () => {
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

export default function DashboardLayout() {
  return (
    <SidebarProvider>
      <RouteGuard>
        <TopLoadingBar />
        <CommandPalette />
        <div className="flex w-full flex-col lg:flex-row">
          <AppSidebar />
          <SidebarInset className="dashboard-scroll scroll-smooth">
            <TopbarAd />
            <VersionUpdateBanner />
            <div className="flex min-h-0 w-full flex-1 flex-col justify-between gap-y-4">
              <PageTransition duration={250} className="flex min-h-0 flex-1 flex-col">
                <Outlet />
              </PageTransition>
              <Footer />
            </div>
          </SidebarInset>
        </div>
      </RouteGuard>
    </SidebarProvider>
  )
}
