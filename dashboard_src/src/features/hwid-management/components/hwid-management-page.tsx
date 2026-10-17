import PageHeader from '@/components/layout/page-header'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Separator } from '@/components/ui/separator'
import { Skeleton } from '@/components/ui/skeleton'
import {
  getHwidManagementSummary,
  getHwidManagementUserDetail,
  listHwidManagementUsers,
  resetHwidUserDevices,
  revokeHwidDevice,
  type HWIDManagementUserBrief,
} from '@/service/hwid-management-api'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'

function SummaryCards({
  deviceCount,
  usersWithDevices,
}: {
  deviceCount: number
  usersWithDevices: number
}) {
  const { t } = useTranslation()
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
      <Card>
        <CardContent className="p-4">
          <p className="text-muted-foreground text-xs">{t('hwidManagement.registeredDevices', 'Registered devices')}</p>
          <p className="text-2xl font-semibold tabular-nums">{deviceCount}</p>
        </CardContent>
      </Card>
      <Card>
        <CardContent className="p-4">
          <p className="text-muted-foreground text-xs">{t('hwidManagement.usersWithDevices', 'Users with devices')}</p>
          <p className="text-2xl font-semibold tabular-nums">{usersWithDevices}</p>
        </CardContent>
      </Card>
    </div>
  )
}

function UserDevicesPanel({ userId, onClose }: { userId: number; onClose: () => void }) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const detailQuery = useQuery({
    queryKey: ['hwid-management-user', userId],
    queryFn: () => getHwidManagementUserDetail(userId),
  })

  const revokeMutation = useMutation({
    mutationFn: revokeHwidDevice,
    onSuccess: () => {
      toast.success(t('hwidManagement.revokeSuccess', 'Device revoked'))
      queryClient.invalidateQueries({ queryKey: ['hwid-management-user', userId] })
      queryClient.invalidateQueries({ queryKey: ['hwid-management-users'] })
      queryClient.invalidateQueries({ queryKey: ['hwid-management-summary'] })
    },
    onError: () => toast.error(t('hwidManagement.revokeFailed', 'Could not revoke device')),
  })

  const resetMutation = useMutation({
    mutationFn: resetHwidUserDevices,
    onSuccess: (data) => {
      toast.success(t('hwidManagement.resetSuccess', { count: data.count, defaultValue: `Reset ${data.count} device(s)` }))
      queryClient.invalidateQueries({ queryKey: ['hwid-management-user', userId] })
      queryClient.invalidateQueries({ queryKey: ['hwid-management-users'] })
      queryClient.invalidateQueries({ queryKey: ['hwid-management-summary'] })
    },
    onError: () => toast.error(t('hwidManagement.resetFailed', 'Could not reset devices')),
  })

  if (detailQuery.isLoading) {
    return <Skeleton className="h-40 w-full" />
  }

  const detail = detailQuery.data
  if (!detail) return null

  return (
    <Card>
      <CardHeader className="flex flex-row items-start justify-between gap-2">
        <div>
          <CardTitle>{detail.user.username}</CardTitle>
          <CardDescription>
            {t('hwidManagement.deviceCount', {
              count: detail.user.device_count,
              status: detail.user.status,
              defaultValue: `${detail.user.device_count} device(s) · ${detail.user.status}`,
            })}
          </CardDescription>
        </div>
        <div className="flex shrink-0 gap-2">
          <Button variant="outline" size="sm" onClick={onClose}>
            {t('close', 'Close')}
          </Button>
          <Button
            variant="destructive"
            size="sm"
            disabled={detail.devices.length === 0 || resetMutation.isPending}
            onClick={() => resetMutation.mutate(userId)}
          >
            {t('hwidManagement.resetAll', 'Reset all')}
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {detail.devices.length === 0 ? (
          <p className="text-muted-foreground text-sm">{t('hwidManagement.noDevices', 'No registered devices.')}</p>
        ) : (
          detail.devices.map((device) => (
            <div key={device.id} className="flex flex-col gap-2 rounded-md border p-3 sm:flex-row sm:items-center sm:justify-between">
              <div className="min-w-0 text-sm">
                <p className="truncate font-mono text-xs">{device.hwid}</p>
                <p className="text-muted-foreground text-xs">
                  {[device.device_model, device.device_os, device.os_version].filter(Boolean).join(' · ') || '—'}
                </p>
                <p className="text-muted-foreground text-xs">
                  {t('hwidManagement.lastSeen', 'Last seen')}: {new Date(device.last_used_at).toLocaleString()}
                </p>
              </div>
              <Button
                variant="outline"
                size="sm"
                className="shrink-0"
                disabled={revokeMutation.isPending}
                onClick={() => revokeMutation.mutate(device.id)}
              >
                {t('hwidManagement.revoke', 'Revoke')}
              </Button>
            </div>
          ))
        )}
      </CardContent>
    </Card>
  )
}

function UserRow({
  row,
  selected,
  onSelect,
}: {
  row: HWIDManagementUserBrief
  selected: boolean
  onSelect: () => void
}) {
  const { t } = useTranslation()
  return (
    <tr
      className={`cursor-pointer border-b last:border-0 ${selected ? 'bg-muted/50' : 'hover:bg-muted/30'}`}
      onClick={onSelect}
    >
      <td className="px-3 py-2 font-medium">{row.username}</td>
      <td className="px-3 py-2 tabular-nums">{row.active_devices}</td>
      <td className="px-3 py-2 text-xs">{row.device_status}</td>
      <td className="px-3 py-2 text-muted-foreground text-xs">
        {row.last_activity_at ? new Date(row.last_activity_at).toLocaleString() : '—'}
      </td>
    </tr>
  )
}

export default function HwidManagementPage() {
  const { t } = useTranslation()
  const [selectedUserId, setSelectedUserId] = useState<number | null>(null)

  const summaryQuery = useQuery({
    queryKey: ['hwid-management-summary'],
    queryFn: getHwidManagementSummary,
    retry: false,
  })

  const usersQuery = useQuery({
    queryKey: ['hwid-management-users'],
    queryFn: () => listHwidManagementUsers({ limit: 100 }),
    enabled: summaryQuery.isSuccess,
    retry: false,
  })

  if (summaryQuery.isError) {
    return (
      <div className="flex w-full flex-col items-start gap-2">
        <PageHeader title="hwidManagement.title" description="hwidManagement.disabledHint" />
        <Separator />
        <p className="px-4 text-muted-foreground text-sm">
          {t(
            'hwidManagement.flagOff',
            'HWID management is not enabled on this deployment (HWID_MANAGEMENT_ENABLED).',
          )}
        </p>
      </div>
    )
  }

  return (
    <div className="flex w-full flex-col items-start gap-2">
      <PageHeader title="hwidManagement.title" description="hwidManagement.subtitle" />
      <Separator />

      <div className="flex w-full flex-col gap-4 px-4 pb-8">
        {summaryQuery.isLoading ? (
          <Skeleton className="h-24 w-full" />
        ) : summaryQuery.data ? (
          <SummaryCards
            deviceCount={summaryQuery.data.device_count}
            usersWithDevices={summaryQuery.data.users_with_devices}
          />
        ) : null}

        {selectedUserId != null && (
          <UserDevicesPanel userId={selectedUserId} onClose={() => setSelectedUserId(null)} />
        )}

        <Card>
          <CardHeader>
            <CardTitle>{t('hwidManagement.userList', 'Users with registered devices')}</CardTitle>
            <CardDescription>
              {t('hwidManagement.userListHint', 'Select a user to revoke devices or reset all registrations in your workspace.')}
            </CardDescription>
          </CardHeader>
          <CardContent className="overflow-x-auto p-0">
            {usersQuery.isLoading ? (
              <div className="p-4">
                <Skeleton className="h-32 w-full" />
              </div>
            ) : (
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b text-left text-muted-foreground text-xs">
                    <th className="px-3 py-2">{t('username', 'Username')}</th>
                    <th className="px-3 py-2">{t('hwidManagement.activeDevices', 'Devices')}</th>
                    <th className="px-3 py-2">{t('status', 'Status')}</th>
                    <th className="px-3 py-2">{t('hwidManagement.lastActivity', 'Last activity')}</th>
                  </tr>
                </thead>
                <tbody>
                  {(usersQuery.data?.users ?? []).map((row) => (
                    <UserRow
                      key={row.user_id}
                      row={row}
                      selected={selectedUserId === row.user_id}
                      onSelect={() => setSelectedUserId(row.user_id)}
                    />
                  ))}
                </tbody>
              </table>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
