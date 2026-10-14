import { AdminDetails, useGetCurrentAdmin } from '@/service/api'
import { selectCurrentAdminDetails, unwrapAdminDetails } from '@/utils/admin-session'
import { useLoaderData } from 'react-router'

export const useAdmin = () => {
  const loaderAdmin = unwrapAdminDetails(useLoaderData())

  const {
    data: admin,
    isLoading,
    error,
    refetch,
  } = useGetCurrentAdmin({
    query: {
      select: selectCurrentAdminDetails,
      initialData: loaderAdmin
        ? {
            data: loaderAdmin,
            status: 200,
            headers: new Headers(),
          }
        : undefined,
      staleTime: 5 * 60 * 1000,
      gcTime: 10 * 60 * 1000,
      refetchOnMount: loaderAdmin ? 'always' : true,
      retry: (failureCount, err) => {
        const status = typeof err === 'object' && err !== null && 'status' in err ? Number((err as { status?: unknown }).status) : undefined
        if (status === 401 || status === 403) return false
        return failureCount < 2
      },
      refetchOnReconnect: 'always',
    },
  })

  const resolvedAdmin: AdminDetails | null = admin ?? loaderAdmin ?? null

  const clearAdmin = () => {
    refetch()
  }

  return {
    admin: resolvedAdmin,
    isLoading: isLoading && !resolvedAdmin,
    error: error as Error | null,
    clearAdmin,
  }
}
