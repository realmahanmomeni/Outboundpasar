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
import { Switch } from '@/components/ui/switch'
import { Input } from '@/components/ui/input'
import { Badge } from '@/components/ui/badge'
import { Skeleton } from '@/components/ui/skeleton'
import { Edit2, Check, X, ShieldAlert, Monitor } from 'lucide-react'
import { useGetPanelHosts, updatePanelHost, PanelHostItem } from '../service/panels-api'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'

export function PanelHostsList({ panelId }: { panelId: string | number }) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const { data: hosts, isLoading, isError } = useGetPanelHosts(panelId)
  
  const [editingId, setEditingId] = useState<number | null>(null)
  const [editForm, setEditForm] = useState<{ display_name: string; multiplier: string }>({
    display_name: '',
    multiplier: '1'
  })

  const updateMutation = useMutation({
    mutationFn: ({ hostId, data }: { hostId: number, data: any }) => updatePanelHost(panelId, hostId, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['/api/panels', String(panelId), 'hosts'] })
      setEditingId(null)
      toast.success(t('panels.hostUpdated', { defaultValue: 'Host updated successfully' }))
    },
    onError: (err: any) => {
      toast.error(err?.message || 'Failed to update host')
    }
  })

  const handleEdit = (host: PanelHostItem) => {
    setEditingId(host.id)
    setEditForm({
      display_name: host.display_name,
      multiplier: host.multiplier !== null ? String(host.multiplier) : '1'
    })
  }

  const handleSave = (hostId: number) => {
    const data: any = {
      display_name: editForm.display_name
    }
    const mult = parseFloat(editForm.multiplier)
    if (!isNaN(mult)) {
      data.multiplier = mult
    }
    updateMutation.mutate({ hostId, data })
  }

  const handleToggleStatus = (hostId: number, currentDisabled: boolean) => {
    updateMutation.mutate({ hostId, data: { is_disabled: !currentDisabled } })
  }

  if (isLoading) {
    return <div className="space-y-4">
      <Skeleton className="h-10 w-full" />
      <Skeleton className="h-10 w-full" />
      <Skeleton className="h-10 w-full" />
    </div>
  }

  if (isError) {
    return <div className="text-sm text-destructive">Failed to load hosts.</div>
  }

  if (!hosts || hosts.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center p-8 text-center text-muted-foreground border rounded-lg bg-muted/20">
        <Monitor className="h-10 w-10 mb-4 opacity-50" />
        <p>No hosts imported yet. Run synchronization to import.</p>
      </div>
    )
  }

  return (
    <div className="rounded-md border">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Display Name (Local)</TableHead>
            <TableHead>Source Config Name</TableHead>
            <TableHead>Group</TableHead>
            <TableHead>Address</TableHead>
            <TableHead>Multiplier</TableHead>
            <TableHead>Status</TableHead>
            <TableHead className="text-right">Actions</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {hosts.map((host) => {
            const isEditing = editingId === host.id
            
            return (
              <TableRow key={host.id}>
                <TableCell>
                  {isEditing ? (
                    <Input
                      value={editForm.display_name}
                      onChange={(e) => setEditForm({ ...editForm, display_name: e.target.value })}
                      className="h-8 max-w-[200px]"
                    />
                  ) : (
                    <span className="font-medium">{host.display_name}</span>
                  )}
                </TableCell>
                <TableCell>
                  <span className="text-xs text-muted-foreground flex items-center gap-1.5">
                    <ShieldAlert className="h-3 w-3" />
                    {host.source_config_name}
                  </span>
                </TableCell>
                <TableCell>
                  {host.group_name ? (
                    <Badge variant="outline" className="text-xs font-normal bg-muted/30">
                      {host.group_name}
                    </Badge>
                  ) : (
                    <span className="text-muted-foreground text-xs">Global</span>
                  )}
                </TableCell>
                <TableCell>
                  <div className="flex flex-col gap-1">
                    {host.address?.map((addr, idx) => (
                      <span key={idx} className="font-mono text-xs text-muted-foreground">
                        {addr}
                      </span>
                    ))}
                  </div>
                </TableCell>
                <TableCell>
                  {isEditing ? (
                    <Input
                      value={editForm.multiplier}
                      onChange={(e) => setEditForm({ ...editForm, multiplier: e.target.value })}
                      type="number"
                      step="0.1"
                      className="h-8 w-20"
                    />
                  ) : (
                    <span>{host.multiplier !== null ? `${host.multiplier}x` : 'N/A'}</span>
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
                      <Button size="icon" variant="ghost" className="h-8 w-8 text-green-500" onClick={() => handleSave(host.id)}>
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
