import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { Skeleton } from '@/components/ui/skeleton'
import { Edit2, Check, X, Monitor } from 'lucide-react'
import { useGetPanelHosts, updatePanelHost, PanelHostItem } from '../service/panels-api'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'

export function PanelHostsList({ panelId }: { panelId: string | number }) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const { data: hosts, isLoading, isError } = useGetPanelHosts(panelId)

  const [editingId, setEditingId] = useState<number | null>(null)
  const [editDisplayName, setEditDisplayName] = useState('')

  const updateMutation = useMutation({
    mutationFn: ({ hostId, data }: { hostId: number; data: Record<string, unknown> }) =>
      updatePanelHost(panelId, hostId, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['/api/panels', String(panelId), 'hosts'] })
      queryClient.invalidateQueries({ queryKey: ['/api/panels', String(panelId)] })
      queryClient.invalidateQueries({ queryKey: ['/api/group-host-options'] })
      setEditingId(null)
      toast.success(t('panels.hostUpdated', { defaultValue: 'Host updated successfully' }))
    },
    onError: (err: { message?: string }) => {
      toast.error(err?.message || 'Failed to update host')
    },
  })

  const handleEdit = (host: PanelHostItem) => {
    setEditingId(host.id)
    setEditDisplayName(host.display_name || '')
  }

  const handleSave = (hostId: number) => {
    updateMutation.mutate({
      hostId,
      data: {
        display_name: editDisplayName.trim(),
      },
    })
  }

  const handleToggleStatus = (hostId: number, currentDisabled: boolean) => {
    updateMutation.mutate({ hostId, data: { is_disabled: !currentDisabled } })
  }

  if (isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-10 w-full" />
        <Skeleton className="h-10 w-full" />
        <Skeleton className="h-10 w-full" />
      </div>
    )
  }

  if (isError) {
    return <div className="text-sm text-destructive">Failed to load hosts.</div>
  }

  if (!hosts || hosts.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center p-8 text-center text-muted-foreground border rounded-lg bg-muted/20">
        <Monitor className="h-10 w-10 mb-4 opacity-50" />
        <p>No destination hosts yet. Run panel sync to import subscription configs.</p>
      </div>
    )
  }

  return (
    <div className="rounded-md border">
      <p className="text-xs text-muted-foreground px-4 py-2 border-b">
        One row per destination subscription config. Edit the host display name only; connection settings come from the
        subscription.
      </p>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Host name</TableHead>
            <TableHead>Status</TableHead>
            <TableHead className="text-right">Actions</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {hosts.map(host => {
            const isEditing = editingId === host.id

            return (
              <TableRow key={host.id}>
                <TableCell className="max-w-[320px]">
                  {isEditing ? (
                    <Input
                      value={editDisplayName}
                      onChange={e => setEditDisplayName(e.target.value)}
                      className="h-8 text-sm"
                    />
                  ) : (
                    <span className="font-medium text-sm">{host.display_name}</span>
                  )}
                </TableCell>
                <TableCell>
                  <div className="flex items-center gap-2">
                    <Switch
                      checked={!host.is_disabled}
                      onCheckedChange={() => handleToggleStatus(host.id, host.is_disabled)}
                      disabled={updateMutation.isPending && updateMutation.variables?.hostId === host.id}
                    />
                    <span className="text-xs text-muted-foreground">
                      {host.is_disabled ? 'Disabled' : 'Enabled'}
                    </span>
                  </div>
                </TableCell>
                <TableCell className="text-right">
                  {isEditing ? (
                    <div className="flex justify-end gap-2">
                      <Button
                        size="icon"
                        variant="ghost"
                        className="h-8 w-8 text-green-500"
                        onClick={() => handleSave(host.id)}
                      >
                        <Check className="h-4 w-4" />
                      </Button>
                      <Button size="icon" variant="ghost" className="h-8 w-8 text-destructive" onClick={() => setEditingId(null)}>
                        <X className="h-4 w-4" />
                      </Button>
                    </div>
                  ) : (
                    <Button size="icon" variant="ghost" className="h-8 w-8" onClick={() => handleEdit(host)}>
                      <Edit2 className="h-4 w-4" />
                    </Button>
                  )}
                </TableCell>
              </TableRow>
            )
          })}
        </TableBody>
      </Table>
    </div>
  )
}
