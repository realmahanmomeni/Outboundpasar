import { useState, useEffect } from 'react'
import { useTranslation } from 'react-i18next'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Server, UserCheck, Layers, Wrench, CheckCircle2, AlertCircle, Loader2 } from 'lucide-react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  getAvailablePanels,
  createTestUser,
  getGroups,
  syncPanel,
  OCPanelItem,
  getTelegramConnection,
  startTelegramConnection,
  confirmTelegramConnection,
  importPanels,
} from '../service/panels-api'
import { Input } from '@/components/ui/input'
import { Checkbox } from '@/components/ui/checkbox'

interface AddPanelWizardModalProps {
  isOpen: boolean
  onOpenChange: (open: boolean) => void
}

export default function AddPanelWizardModal({
  isOpen,
  onOpenChange,
}: AddPanelWizardModalProps) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()

  const [step, setStep] = useState<number>(0)
  const [selectedPanelIds, setSelectedPanelIds] = useState<number[]>([])
  const [localPanelId, setLocalPanelId] = useState<number | null>(null)
  const [selectedGroupIds, setSelectedGroupIds] = useState<string[]>([])
  const [botUrl, setBotUrl] = useState<string | null>(null)
  const [connectCode, setConnectCode] = useState('')
  const [connectionStatus, setConnectionStatus] = useState<string>('loading')
  
  const [errorMsg, setErrorMsg] = useState<string | null>(null)

  // Reset state when opened
  useEffect(() => {
    if (isOpen) {
      setStep(0)
      setSelectedPanelIds([])
      setLocalPanelId(null)
      setSelectedGroupIds([])
      setBotUrl(null)
      setConnectCode('')
      setErrorMsg(null)
      getTelegramConnection()
        .then((s) => setConnectionStatus(s.status))
        .catch(() => setConnectionStatus('none'))
    }
  }, [isOpen])

  // Queries
  const { data: availablePanelsData, isLoading: isLoadingPanels, isError: isErrorPanels, refetch: refetchPanels } = useQuery({
    queryKey: ['available-panels'],
    queryFn: getAvailablePanels,
    enabled: isOpen && step === 1 && connectionStatus === 'active',
  })
  
  const { data: groupsData, isLoading: isLoadingGroups, isError: isErrorGroups, refetch: refetchGroups } = useQuery({
    queryKey: ['panel-groups', localPanelId],
    queryFn: () => getGroups(localPanelId!),
    enabled: isOpen && step === 3 && !!localPanelId,
  })

  // Mutations
  const importPanelsMut = useMutation({
    mutationFn: importPanels,
    onSuccess: (data) => {
      const first = data.imported[0]
      if (first) setLocalPanelId(first.panel_id)
      setStep(2)
      setErrorMsg(null)
    },
    onError: (err: any) => setErrorMsg(err.message || 'Failed to import panels'),
  })

  const startConnectMut = useMutation({
    mutationFn: startTelegramConnection,
    onSuccess: (data) => {
      setBotUrl(data.bot_url)
      setErrorMsg(null)
    },
    onError: (err: any) => setErrorMsg(err.message || 'Failed to start Telegram connection'),
  })

  const confirmConnectMut = useMutation({
    mutationFn: () => confirmTelegramConnection(connectCode.trim()),
    onSuccess: () => {
      setConnectionStatus('active')
      setStep(1)
      setErrorMsg(null)
    },
    onError: (err: any) => setErrorMsg(err.message || 'Invalid or expired code'),
  })

  const testUserMut = useMutation({
    mutationFn: createTestUser,
    onSuccess: () => {
      setStep(3)
      setErrorMsg(null)
    },
    onError: (err: any) => setErrorMsg(err.message || 'Failed to create test user')
  })

  const syncMut = useMutation({
    mutationFn: (data: { panelId: number, req: any }) => syncPanel(data.panelId, data.req),
    onSuccess: () => {
      setStep(6)
      setErrorMsg(null)
      queryClient.invalidateQueries({ queryKey: ['/api/panels'] })
    },
    onError: (err: any) => setErrorMsg(err.message || 'Failed to sync configs')
  })

  // Handlers
  const handleImportPanels = () => {
    if (selectedPanelIds.length === 0) return
    setErrorMsg(null)
    importPanelsMut.mutate(selectedPanelIds)
  }

  const handleCreateTestUser = () => {
    if (!localPanelId) return
    setErrorMsg(null)
    testUserMut.mutate(localPanelId)
  }
  
  const handleSelectAllGroups = () => {
    if (groupsData?.groups) {
      setSelectedGroupIds(groupsData.groups.map(g => g.id))
    }
  }
  
  const handleDeselectAllGroups = () => {
    setSelectedGroupIds([])
  }

  const handleSyncConfigs = () => {
    if (!localPanelId) return
    if (selectedGroupIds.length === 0) {
      setErrorMsg('At least one group must be selected.')
      return
    }
    setErrorMsg(null)
    const groupNames: Record<string, string> = {}
    groupsData?.groups?.forEach(g => {
      groupNames[g.id] = g.name
    })
    
    syncMut.mutate({ panelId: localPanelId, req: { selected_group_ids: selectedGroupIds, group_names: groupNames } })
  }

  return (
    <Dialog open={isOpen} onOpenChange={(val) => !importPanelsMut.isPending && !testUserMut.isPending && !syncMut.isPending && onOpenChange(val)}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <div className="flex items-center gap-2">
            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10 text-primary">
              <Server className="h-5 w-5" />
            </div>
            <div>
              <DialogTitle>Add Purchased Panel</DialogTitle>
              <DialogDescription className="text-xs">
                Integration Wizard
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>

        <div className="space-y-4 py-2">
          {errorMsg && (
            <div className="flex items-center gap-2 rounded-md bg-destructive/10 p-3 text-sm text-destructive">
              <AlertCircle className="h-4 w-4" />
              <span>{errorMsg}</span>
            </div>
          )}

          {step === 0 && (
            <div className="space-y-3">
              <h3 className="font-semibold text-sm flex items-center gap-2">
                <span className="flex h-5 w-5 rounded-full bg-primary text-primary-foreground items-center justify-center text-xs">0</span>
                Connect Outbound Center Telegram
              </h3>
              {connectionStatus === 'active' ? (
                <p className="text-sm text-muted-foreground">Telegram account connected. Continue to import your panels.</p>
              ) : (
                <>
                  <p className="text-sm text-muted-foreground">
                    Open the Outbound Center bot, confirm the connection, then enter the 5-digit code here.
                  </p>
                  <Button onClick={() => startConnectMut.mutate()} disabled={startConnectMut.isPending}>
                    {startConnectMut.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                    Connect Telegram
                  </Button>
                  {botUrl && (
                    <a href={botUrl} target="_blank" rel="noreferrer" className="text-sm text-primary underline block">
                      Open Outbound Center bot
                    </a>
                  )}
                  <Input
                    placeholder="5-digit code"
                    value={connectCode}
                    onChange={(e) => setConnectCode(e.target.value.replace(/\D/g, '').slice(0, 5))}
                    maxLength={5}
                  />
                </>
              )}
            </div>
          )}

          {step === 1 && (
            <div className="space-y-3">
              <h3 className="font-semibold text-sm flex items-center gap-2"><span className="flex h-5 w-5 rounded-full bg-primary text-primary-foreground items-center justify-center text-xs">1</span> Select panels to import</h3>
              {isLoadingPanels ? (
                <div className="flex items-center gap-2 text-sm text-muted-foreground"><Loader2 className="h-4 w-4 animate-spin" /> Loading panels...</div>
              ) : isErrorPanels ? (
                <div className="text-sm text-destructive">Failed to load panels. <Button variant="link" onClick={() => refetchPanels()}>Retry</Button></div>
              ) : (
                <div className="space-y-2 max-h-48 overflow-y-auto">
                  {availablePanelsData?.items?.length === 0 && <p className="text-sm text-muted-foreground">No available panels found.</p>}
                  {availablePanelsData?.items?.map(p => (
                    <div 
                      key={p.id} 
                      className={`flex items-center justify-between p-3 border rounded-md cursor-pointer transition-colors ${selectedPanelIds.includes(p.id) ? 'border-primary bg-primary/5' : 'hover:bg-muted'}`}
                      onClick={() => {
                        setSelectedPanelIds((prev) =>
                          prev.includes(p.id) ? prev.filter((id) => id !== p.id) : [...prev, p.id]
                        )
                      }}
                    >
                      <div className="flex items-center gap-3">
                        <Checkbox checked={selectedPanelIds.includes(p.id)} />
                        <Server className="h-4 w-4 text-muted-foreground" />
                        <span className="text-sm font-medium">{p.name}</span>
                      </div>
                      <span className="text-xs capitalize px-2 py-1 bg-muted rounded-full">{p.status}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {step === 2 && (
            <div className="space-y-3">
              <h3 className="font-semibold text-sm flex items-center gap-2"><span className="flex h-5 w-5 rounded-full bg-primary text-primary-foreground items-center justify-center text-xs">2</span> Create / Reuse Test User</h3>
              <p className="text-sm text-muted-foreground">We need to provision or reuse a test identity on the external panel for discovery and synchronization.</p>
              <div className="flex items-center gap-3 p-3 border rounded-md bg-muted/30">
                <UserCheck className="h-5 w-5 text-muted-foreground" />
                <div className="flex flex-col">
                  <span className="text-sm font-medium">Test Identity</span>
                  <span className="text-xs text-muted-foreground">Required for reading groups and configs</span>
                </div>
              </div>
            </div>
          )}

          {step === 3 && (
            <div className="space-y-3">
              <h3 className="font-semibold text-sm flex items-center gap-2"><span className="flex h-5 w-5 rounded-full bg-primary text-primary-foreground items-center justify-center text-xs">3</span> Fetch Groups</h3>
              {isLoadingGroups ? (
                <div className="flex items-center gap-2 text-sm text-muted-foreground"><Loader2 className="h-4 w-4 animate-spin" /> Fetching groups from panel...</div>
              ) : isErrorGroups ? (
                <div className="text-sm text-destructive">Failed to fetch groups. <Button variant="link" onClick={() => refetchGroups()}>Retry</Button></div>
              ) : (
                <div className="space-y-2">
                  <p className="text-sm text-muted-foreground">Groups fetched successfully. Proceed to select target groups.</p>
                  <Button size="sm" onClick={() => setStep(4)}>Select Groups</Button>
                </div>
              )}
            </div>
          )}

          {step === 4 && (
            <div className="space-y-3">
              <h3 className="font-semibold text-sm flex items-center gap-2"><span className="flex h-5 w-5 rounded-full bg-primary text-primary-foreground items-center justify-center text-xs">4</span> Select Groups</h3>
              <p className="text-sm text-muted-foreground">Select the external groups whose configs you want to synchronize into PasarGuard.</p>
              
              <div className="flex gap-2 mb-2">
                <Button size="sm" variant="outline" onClick={handleSelectAllGroups}>Select All</Button>
                <Button size="sm" variant="outline" onClick={handleDeselectAllGroups}>Deselect All</Button>
              </div>
              
              <div className="space-y-2 max-h-48 overflow-y-auto border p-2 rounded-md">
                {groupsData?.groups?.length === 0 && <p className="text-sm text-muted-foreground p-2">No groups found on the panel.</p>}
                {groupsData?.groups?.map(g => (
                  <div key={g.id} className="flex items-center space-x-2 p-2 hover:bg-muted/50 rounded-md">
                    <Checkbox 
                      id={`group-${g.id}`} 
                      checked={selectedGroupIds.includes(g.id)}
                      onCheckedChange={(checked) => {
                        if (checked) {
                          setSelectedGroupIds([...selectedGroupIds, g.id])
                        } else {
                          setSelectedGroupIds(selectedGroupIds.filter(id => id !== g.id))
                        }
                      }}
                    />
                    <label htmlFor={`group-${g.id}`} className="text-sm font-medium leading-none peer-disabled:cursor-not-allowed peer-disabled:opacity-70 cursor-pointer">
                      {g.name}
                    </label>
                  </div>
                ))}
              </div>
            </div>
          )}

          {step === 5 && (
            <div className="space-y-3">
              <h3 className="font-semibold text-sm flex items-center gap-2"><span className="flex h-5 w-5 rounded-full bg-primary text-primary-foreground items-center justify-center text-xs">5</span> Sync Configs & Create Hosts</h3>
              <p className="text-sm text-muted-foreground">We will now retrieve configs for the selected groups and map them to independent PasarGuard Hosts.</p>
              <div className="flex items-center gap-3 p-3 border rounded-md bg-muted/30">
                <Wrench className="h-5 w-5 text-muted-foreground" />
                <div className="flex flex-col">
                  <span className="text-sm font-medium">{selectedGroupIds.length} group(s) selected</span>
                  <span className="text-xs text-muted-foreground">Configs will be synced</span>
                </div>
              </div>
            </div>
          )}
          
          {step === 6 && (
            <div className="flex flex-col items-center justify-center py-6 space-y-3">
              <div className="h-12 w-12 bg-green-100 dark:bg-green-900/30 text-green-600 rounded-full flex items-center justify-center">
                <CheckCircle2 className="h-6 w-6" />
              </div>
              <h3 className="text-lg font-semibold">Panel Added Successfully</h3>
              <p className="text-sm text-muted-foreground text-center">
                The panel, groups, configs and virtual hosts have been integrated and synced.
              </p>
            </div>
          )}

        </div>

        <DialogFooter>
          {step > 0 && step < 6 && (
            <Button variant="outline" onClick={() => setStep(step - 1)} disabled={importPanelsMut.isPending || testUserMut.isPending || syncMut.isPending}>
              Back
            </Button>
          )}
          {step === 0 && (
            <Button
              onClick={() => (connectionStatus === 'active' ? setStep(1) : confirmConnectMut.mutate())}
              disabled={connectionStatus === 'active' ? false : connectCode.length !== 5 || confirmConnectMut.isPending}
            >
              {confirmConnectMut.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              {connectionStatus === 'active' ? 'Continue' : 'Confirm code'}
            </Button>
          )}
          {step === 1 && (
            <Button 
              onClick={handleImportPanels} 
              disabled={selectedPanelIds.length === 0 || importPanelsMut.isPending}
            >
              {importPanelsMut.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              Import selected
            </Button>
          )}
          {step === 2 && (
            <Button 
              onClick={handleCreateTestUser} 
              disabled={testUserMut.isPending}
            >
              {testUserMut.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              Create Test User
            </Button>
          )}
          {step === 4 && (
            <Button 
              onClick={() => {
                if (selectedGroupIds.length === 0) {
                  setErrorMsg('At least one group must be selected.')
                  return
                }
                setErrorMsg(null)
                setStep(5)
              }} 
            >
              Continue
            </Button>
          )}
          {step === 5 && (
            <Button 
              onClick={handleSyncConfigs} 
              disabled={syncMut.isPending}
            >
              {syncMut.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              Sync Configs & Hosts
            </Button>
          )}
          {(step === 0 || step === 6) && (
            <Button variant="outline" onClick={() => onOpenChange(false)}>
              {step === 6 ? 'Done' : 'Cancel'}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
