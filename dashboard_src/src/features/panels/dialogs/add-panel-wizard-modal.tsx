import { useState, useEffect, useCallback } from 'react'
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
import { Server, Layers, Wrench, CheckCircle2, AlertCircle, Loader2, Unplug } from 'lucide-react'
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
  revokeTelegramConnection,
  importPanels,
  formatApiError,
  getPanels,
  type PanelItem,
} from '../service/panels-api'
import { Input } from '@/components/ui/input'
import { Checkbox } from '@/components/ui/checkbox'

interface AddPanelWizardModalProps {
  isOpen: boolean
  onOpenChange: (open: boolean) => void
}

type PanelWorkItem = { panelId: number; name: string }
type TestUserSetupPhase = 'idle' | 'running' | 'ready' | 'error'

export default function AddPanelWizardModal({
  isOpen,
  onOpenChange,
}: AddPanelWizardModalProps) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()

  const [step, setStep] = useState<number>(0)
  const [selectedPanelIds, setSelectedPanelIds] = useState<number[]>([])
  const [panelWorkQueue, setPanelWorkQueue] = useState<PanelWorkItem[]>([])
  const [panelWorkIndex, setPanelWorkIndex] = useState(0)
  const [localPanelId, setLocalPanelId] = useState<number | null>(null)
  const [selectedGroupIds, setSelectedGroupIds] = useState<string[]>([])
  const [botUrl, setBotUrl] = useState<string | null>(null)
  const [connectCode, setConnectCode] = useState('')
  const [connectionStatus, setConnectionStatus] = useState<string>('loading')
  const [telegramUserId, setTelegramUserId] = useState<number | null>(null)

  const [errorMsg, setErrorMsg] = useState<string | null>(null)
  const [pendingLocalPanels, setPendingLocalPanels] = useState<PanelItem[]>([])
  const [testUserSetupPhase, setTestUserSetupPhase] = useState<TestUserSetupPhase>('idle')

  const isTelegramConnected = connectionStatus === 'active'

  const refreshTelegramStatus = useCallback(() => {
    return getTelegramConnection()
      .then((s) => {
        const connected = s.status === 'active' || Boolean(s.active)
        setConnectionStatus(connected ? 'active' : s.status || 'none')
        setTelegramUserId(s.telegram_user_id ?? null)
        if (s.bot_url) setBotUrl(s.bot_url)
      })
      .catch(() => setConnectionStatus('none'))
  }, [])

  const resetWizardState = useCallback(() => {
    setStep(0)
    setSelectedPanelIds([])
    setPanelWorkQueue([])
    setPanelWorkIndex(0)
    setLocalPanelId(null)
    setSelectedGroupIds([])
    setConnectCode('')
    setErrorMsg(null)
    setTestUserSetupPhase('idle')
  }, [])

  useEffect(() => {
    if (!isOpen) return
    refreshTelegramStatus()
    resetWizardState()
    getPanels()
      .then((rows) => {
        const pending = rows.filter((p) => (p.sync_status || '').toLowerCase() === 'pending')
        setPendingLocalPanels(pending)
      })
      .catch(() => setPendingLocalPanels([]))
  }, [isOpen, resetWizardState, refreshTelegramStatus])

  const resumePendingSetup = () => {
    if (pendingLocalPanels.length === 0) return
    const queue: PanelWorkItem[] = pendingLocalPanels.map((p) => ({
      panelId: p.id,
      name: p.name,
    }))
    setPanelWorkQueue(queue)
    setPanelWorkIndex(0)
    setLocalPanelId(queue[0]?.panelId ?? null)
    setSelectedGroupIds([])
    setTestUserSetupPhase('idle')
    setStep(2)
    setErrorMsg(null)
  }

  const currentWorkItem = panelWorkQueue[panelWorkIndex] ?? null
  const activeLocalPanelId = localPanelId ?? currentWorkItem?.panelId ?? null
  const panelsRemaining = panelWorkQueue.length - panelWorkIndex
  const panelsTotal = panelWorkQueue.length

  const { data: availablePanelsData, isLoading: isLoadingPanels, isError: isErrorPanels, refetch: refetchPanels } = useQuery({
    queryKey: ['available-panels'],
    queryFn: getAvailablePanels,
    enabled: isOpen && step === 1 && isTelegramConnected,
  })

  const { data: groupsData, isLoading: isLoadingGroups, isError: isErrorGroups, refetch: refetchGroups } = useQuery({
    queryKey: ['panel-groups', activeLocalPanelId],
    queryFn: () => getGroups(activeLocalPanelId!),
    enabled: isOpen && step === 2 && testUserSetupPhase === 'ready' && !!activeLocalPanelId,
  })

  useEffect(() => {
    if (!isOpen || step !== 2 || !activeLocalPanelId) return
    if (
      testUserSetupPhase === 'running' ||
      testUserSetupPhase === 'ready' ||
      testUserSetupPhase === 'error'
    ) {
      return
    }

    let cancelled = false
    setTestUserSetupPhase('running')
    setErrorMsg(null)
    createTestUser(activeLocalPanelId)
      .then(() => {
        if (!cancelled) setTestUserSetupPhase('ready')
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setTestUserSetupPhase('error')
          setErrorMsg(formatApiError(err, 'Failed to prepare panel for group discovery'))
        }
      })
    return () => {
      cancelled = true
    }
  }, [isOpen, step, activeLocalPanelId, testUserSetupPhase])

  const beginPanelWorkflow = (imported: { panel_id: number; subscription_id: number }[], panelNames: Map<number, string>) => {
    const queue: PanelWorkItem[] = imported.map((row) => ({
      panelId: row.panel_id,
      name: panelNames.get(row.subscription_id) ?? `Panel ${row.subscription_id}`,
    }))
    setPanelWorkQueue(queue)
    setPanelWorkIndex(0)
    setLocalPanelId(queue[0]?.panelId ?? null)
    setSelectedGroupIds([])
    setTestUserSetupPhase('idle')
    setStep(2)
    setErrorMsg(null)
  }

  const advanceToNextPanelOrFinish = () => {
    queryClient.invalidateQueries({ queryKey: ['/api/panels'] })
    queryClient.invalidateQueries({ queryKey: ['available-panels'] })
    const nextIndex = panelWorkIndex + 1
    if (nextIndex < panelWorkQueue.length) {
      setPanelWorkIndex(nextIndex)
      setLocalPanelId(panelWorkQueue[nextIndex].panelId)
      setSelectedGroupIds([])
      setTestUserSetupPhase('idle')
      setStep(2)
      setErrorMsg(null)
    } else {
      setStep(5)
      setErrorMsg(null)
    }
  }

  const importPanelsMut = useMutation({
    mutationFn: importPanels,
    onSuccess: (data) => {
      const nameBySubId = new Map<number, string>()
      availablePanelsData?.items?.forEach((p) => nameBySubId.set(p.id, p.name))
      beginPanelWorkflow(data.imported, nameBySubId)
    },
    onError: (err: unknown) => setErrorMsg(formatApiError(err, 'Failed to import panels')),
  })

  const startConnectMut = useMutation({
    mutationFn: startTelegramConnection,
    onSuccess: (data) => {
      setBotUrl(data.bot_url)
      setConnectionStatus('pending')
      setErrorMsg(null)
    },
    onError: (err: unknown) => setErrorMsg(formatApiError(err, 'Failed to start Telegram connection')),
  })

  const confirmConnectMut = useMutation({
    mutationFn: () => confirmTelegramConnection(connectCode.trim()),
    onSuccess: async () => {
      await refreshTelegramStatus()
      setStep(1)
      setErrorMsg(null)
    },
    onError: (err: unknown) => setErrorMsg(formatApiError(err, 'Invalid or expired code')),
  })

  const disconnectMut = useMutation({
    mutationFn: revokeTelegramConnection,
    onSuccess: async () => {
      setConnectionStatus('none')
      setTelegramUserId(null)
      setBotUrl(null)
      setConnectCode('')
      setStep(0)
      setErrorMsg(null)
      queryClient.invalidateQueries({ queryKey: ['available-panels'] })
      queryClient.invalidateQueries({ queryKey: ['/api/panels'] })
    },
    onError: (err: unknown) => setErrorMsg(formatApiError(err, 'Failed to disconnect Telegram')),
  })

  const syncMut = useMutation({
    mutationFn: (data: { panelId: number; req: { selected_group_ids: string[]; group_names: Record<string, string> } }) =>
      syncPanel(data.panelId, data.req),
    onSuccess: (data: {
      status?: string
      warnings?: string[]
      destination_hosts_total?: number
      catalog_configs_total?: number
    }) => {
      if (data?.warnings?.length) {
        setErrorMsg(data.warnings.join(' '))
      }
      if (data?.status === 'partial') {
        setErrorMsg(
          (data.warnings ?? []).join(' ') ||
            `Partial sync: ${data.destination_hosts_total ?? 0} destination hosts from ${data.catalog_configs_total ?? 0} catalog configs.`,
        )
      }
      advanceToNextPanelOrFinish()
    },
    onError: (err: unknown) => setErrorMsg(formatApiError(err, 'Failed to sync configs')),
  })

  const togglePanelSelection = (panel: OCPanelItem) => {
    if (panel.already_imported) return
    setSelectedPanelIds((prev) =>
      prev.includes(panel.id) ? prev.filter((id) => id !== panel.id) : [...prev, panel.id]
    )
  }

  const handleImportPanels = () => {
    const selectable = selectedPanelIds.filter((id) => {
      const meta = availablePanelsData?.items?.find((p) => p.id === id)
      return meta && !meta.already_imported
    })
    if (selectable.length === 0) return
    setErrorMsg(null)
    importPanelsMut.mutate(selectable)
  }

  const handleSelectAllGroups = () => {
    if (groupsData?.groups) {
      setSelectedGroupIds(groupsData.groups.map((g) => g.id))
    }
  }

  const handleDeselectAllGroups = () => {
    setSelectedGroupIds([])
  }

  const handleSyncConfigs = () => {
    if (!activeLocalPanelId) return
    if (selectedGroupIds.length === 0) {
      setErrorMsg('At least one group must be selected.')
      return
    }
    setErrorMsg(null)
    const groupNames: Record<string, string> = {}
    groupsData?.groups?.forEach((g) => {
      groupNames[g.id] = g.name
    })

    syncMut.mutate({
      panelId: activeLocalPanelId,
      req: { selected_group_ids: selectedGroupIds, group_names: groupNames },
    })
  }

  const workflowBusy =
    importPanelsMut.isPending ||
    testUserSetupPhase === 'running' ||
    syncMut.isPending ||
    disconnectMut.isPending

  return (
    <Dialog open={isOpen} onOpenChange={(val) => !workflowBusy && onOpenChange(val)}>
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
                {panelsTotal > 1 && step >= 2 && step < 5 && (
                  <span className="block text-muted-foreground mt-0.5">
                    Panel {panelWorkIndex + 1} of {panelsTotal}
                    {currentWorkItem ? `: ${currentWorkItem.name}` : ''}
                  </span>
                )}
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>

        {errorMsg ? (
          <div className="mb-3 flex items-center gap-2 rounded-md bg-destructive/10 p-3 text-sm text-destructive">
            <AlertCircle className="h-4 w-4 shrink-0" />
            <span>{errorMsg}</span>
          </div>
        ) : null}

        <div className="space-y-4 py-2 min-h-[12rem]">
          {step === 0 && (
            <div className="space-y-3">
              <h3 className="font-semibold text-sm flex items-center gap-2">
                <span className="flex h-5 w-5 rounded-full bg-primary text-primary-foreground items-center justify-center text-xs">0</span>
                Connect Outbound Center Telegram
              </h3>
              {isTelegramConnected ? (
                <div className="space-y-3">
                  {pendingLocalPanels.length > 0 && (
                    <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm">
                      <p className="font-medium text-foreground">
                        {pendingLocalPanels.length} panel(s) need setup (groups / sync).
                      </p>
                      <p className="text-muted-foreground text-xs mt-1">
                        Import created these panels but setup was not finished. Resume without re-importing.
                      </p>
                      <Button type="button" size="sm" className="mt-2" variant="secondary" onClick={resumePendingSetup}>
                        Resume pending setup
                      </Button>
                    </div>
                  )}
                  <p className="text-sm text-muted-foreground">
                    Telegram account connected
                    {telegramUserId ? ` (ID ${telegramUserId})` : ''}. Continue to import your panels, or disconnect to link a different account.
                  </p>
                  <Button
                    variant="outline"
                    onClick={() => disconnectMut.mutate()}
                    disabled={disconnectMut.isPending}
                    className="gap-2"
                  >
                    {disconnectMut.isPending ? (
                      <Loader2 className="h-4 w-4 animate-spin" />
                    ) : (
                      <Unplug className="h-4 w-4" />
                    )}
                    Disconnect Telegram
                  </Button>
                </div>
              ) : (
                <>
                  <ol className="text-sm text-muted-foreground list-decimal list-inside space-y-1.5">
                    <li>Click <strong className="text-foreground">Connect Telegram</strong> to register this session.</li>
                    <li>Open the Outbound Center bot and confirm the account link.</li>
                    <li>Enter the 5-digit code the bot sends you below.</li>
                  </ol>
                  <p className="text-xs text-muted-foreground">
                    Phone login and Telegram 2FA (if enabled) are handled inside Telegram with Outbound Center—not in this dashboard.
                  </p>
                  <Button onClick={() => startConnectMut.mutate()} disabled={startConnectMut.isPending || isTelegramConnected}>
                    {startConnectMut.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                    Connect Telegram
                  </Button>
                  {botUrl && (
                    <a href={botUrl} target="_blank" rel="noreferrer" className="text-sm text-primary underline block">
                      Open Outbound Center bot
                    </a>
                  )}
                  <div className="space-y-1.5 pt-1">
                    <label className="text-xs font-medium text-foreground">Verification code</label>
                    <Input
                      placeholder="5-digit code"
                      value={connectCode}
                      onChange={(e) => setConnectCode(e.target.value.replace(/\D/g, '').slice(0, 5))}
                      maxLength={5}
                      inputMode="numeric"
                      autoComplete="one-time-code"
                    />
                  </div>
                </>
              )}
            </div>
          )}

          {step === 1 && (
            <div className="space-y-3">
              <h3 className="font-semibold text-sm flex items-center gap-2">
                <span className="flex h-5 w-5 rounded-full bg-primary text-primary-foreground items-center justify-center text-xs">1</span>
                Select panels to import
              </h3>
              <p className="text-xs text-muted-foreground">
                You can select multiple panels; each will be configured one after another (groups, sync).
              </p>
              {isLoadingPanels ? (
                <div className="flex items-center gap-2 text-sm text-muted-foreground">
                  <Loader2 className="h-4 w-4 animate-spin" /> Loading panels...
                </div>
              ) : isErrorPanels ? (
                <div className="text-sm text-destructive">
                  Failed to load panels. <Button variant="link" onClick={() => refetchPanels()}>Retry</Button>
                </div>
              ) : (
                <div className="space-y-2 max-h-48 overflow-y-auto">
                  {availablePanelsData?.items?.length === 0 && (
                    <p className="text-sm text-muted-foreground">No available panels found.</p>
                  )}
                  {availablePanelsData?.items?.map((p) => {
                    const imported = Boolean(p.already_imported)
                    const selected = selectedPanelIds.includes(p.id)
                    return (
                      <div
                        key={p.id}
                        role="button"
                        tabIndex={imported ? -1 : 0}
                        aria-disabled={imported}
                        className={`flex items-center justify-between p-3 border rounded-md transition-colors ${
                          imported
                            ? 'opacity-60 cursor-not-allowed bg-muted/40'
                            : selected
                              ? 'border-primary bg-primary/5 cursor-pointer'
                              : 'hover:bg-muted cursor-pointer'
                        }`}
                        onClick={() => togglePanelSelection(p)}
                        onKeyDown={(e) => {
                          if (imported) return
                          if (e.key === 'Enter' || e.key === ' ') {
                            e.preventDefault()
                            togglePanelSelection(p)
                          }
                        }}
                      >
                        <div className="flex items-center gap-3 min-w-0">
                          <Checkbox checked={selected} disabled={imported} />
                          <Server className="h-4 w-4 text-muted-foreground shrink-0" />
                          <span className={`text-sm font-medium truncate ${imported ? 'line-through' : ''}`}>{p.name}</span>
                          {imported && (
                            <span className="text-xs text-muted-foreground shrink-0">Already added</span>
                          )}
                        </div>
                        <span className="text-xs capitalize px-2 py-1 bg-muted rounded-full shrink-0">{p.status}</span>
                      </div>
                    )
                  })}
                </div>
              )}
            </div>
          )}

          {step === 2 && (
            <div className="space-y-3">
              <h3 className="font-semibold text-sm flex items-center gap-2">
                <span className="flex h-5 w-5 rounded-full bg-primary text-primary-foreground items-center justify-center text-xs">2</span>
                Fetch Groups
              </h3>
              {currentWorkItem && (
                <p className="text-xs font-medium text-foreground">{currentWorkItem.name}</p>
              )}
              {testUserSetupPhase === 'running' && (
                <div className="flex items-center gap-2 text-sm text-muted-foreground">
                  <Loader2 className="h-4 w-4 animate-spin" /> Preparing panel for discovery…
                </div>
              )}
              {testUserSetupPhase === 'error' && (
                <div className="text-sm text-muted-foreground">
                  Panel setup did not complete. Fix the error above, then{' '}
                  <Button
                    variant="link"
                    className="h-auto p-0"
                    onClick={() => setTestUserSetupPhase('idle')}
                  >
                    retry
                  </Button>
                  .
                </div>
              )}
              {testUserSetupPhase === 'ready' && isLoadingGroups && (
                <div className="flex items-center gap-2 text-sm text-muted-foreground">
                  <Loader2 className="h-4 w-4 animate-spin" /> Fetching groups from panel...
                </div>
              )}
              {testUserSetupPhase === 'ready' && isErrorGroups && (
                <div className="text-sm text-destructive">
                  Failed to fetch groups. <Button variant="link" onClick={() => refetchGroups()}>Retry</Button>
                </div>
              )}
              {testUserSetupPhase === 'ready' && !isLoadingGroups && !isErrorGroups && (
                <div className="space-y-2">
                  <p className="text-sm text-muted-foreground">Groups fetched successfully. Proceed to select target groups.</p>
                </div>
              )}
            </div>
          )}

          {step === 3 && (
            <div className="space-y-3">
              <h3 className="font-semibold text-sm flex items-center gap-2">
                <span className="flex h-5 w-5 rounded-full bg-primary text-primary-foreground items-center justify-center text-xs">3</span>
                Select Groups
              </h3>
              <p className="text-sm text-muted-foreground">
                Select the external groups whose configs you want to synchronize into OutboundCenter.
              </p>

              <div className="flex gap-2 mb-2">
                <Button size="sm" variant="outline" onClick={handleSelectAllGroups}>Select All</Button>
                <Button size="sm" variant="outline" onClick={handleDeselectAllGroups}>Deselect All</Button>
              </div>

              <div className="space-y-2 max-h-48 overflow-y-auto border p-2 rounded-md">
                {groupsData?.groups?.length === 0 && (
                  <p className="text-sm text-muted-foreground p-2">No groups found on the panel.</p>
                )}
                {groupsData?.groups?.map((g) => (
                  <div key={g.id} className="flex items-center space-x-2 p-2 hover:bg-muted/50 rounded-md">
                    <Checkbox
                      id={`group-${g.id}`}
                      checked={selectedGroupIds.includes(g.id)}
                      onCheckedChange={(checked) => {
                        if (checked) {
                          setSelectedGroupIds([...selectedGroupIds, g.id])
                        } else {
                          setSelectedGroupIds(selectedGroupIds.filter((id) => id !== g.id))
                        }
                      }}
                    />
                    <label
                      htmlFor={`group-${g.id}`}
                      className="text-sm font-medium leading-none peer-disabled:cursor-not-allowed peer-disabled:opacity-70 cursor-pointer"
                    >
                      {g.name}
                    </label>
                  </div>
                ))}
              </div>
            </div>
          )}

          {step === 4 && (
            <div className="space-y-3">
              <h3 className="font-semibold text-sm flex items-center gap-2">
                <span className="flex h-5 w-5 rounded-full bg-primary text-primary-foreground items-center justify-center text-xs">4</span>
                Sync Configs & Create Hosts
              </h3>
              <p className="text-sm text-muted-foreground">
                We will now retrieve configs for the selected groups and map them to independent OutboundCenter hosts.
              </p>
              <div className="flex items-center gap-3 p-3 border rounded-md bg-muted/30">
                <Wrench className="h-5 w-5 text-muted-foreground" />
                <div className="flex flex-col">
                  <span className="text-sm font-medium">{selectedGroupIds.length} group(s) selected</span>
                  <span className="text-xs text-muted-foreground">Configs will be synced</span>
                </div>
              </div>
            </div>
          )}

          {step === 5 && (
            <div className="flex flex-col items-center justify-center py-6 space-y-3">
              <div className="h-12 w-12 bg-green-100 dark:bg-green-900/30 text-green-600 rounded-full flex items-center justify-center">
                <CheckCircle2 className="h-6 w-6" />
              </div>
              <h3 className="text-lg font-semibold">Panel Added Successfully</h3>
              <p className="text-sm text-muted-foreground text-center">
                {panelsTotal > 1
                  ? `All ${panelsTotal} panels have been integrated and synced.`
                  : 'The panel, groups, configs and virtual hosts have been integrated and synced.'}
              </p>
            </div>
          )}
        </div>

        <DialogFooter>
          {step > 0 && step < 5 && (
            <Button variant="outline" onClick={() => setStep(step - 1)} disabled={workflowBusy}>
              Back
            </Button>
          )}
          {step === 0 && (
            <>
              {isTelegramConnected ? (
                <Button onClick={() => setStep(1)} disabled={disconnectMut.isPending}>
                  Continue
                </Button>
              ) : (
                <Button
                  onClick={() => confirmConnectMut.mutate()}
                  disabled={connectCode.length !== 5 || confirmConnectMut.isPending}
                >
                  {confirmConnectMut.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                  Confirm code
                </Button>
              )}
            </>
          )}
          {step === 1 && (
            <Button onClick={handleImportPanels} disabled={selectedPanelIds.length === 0 || importPanelsMut.isPending}>
              {importPanelsMut.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              Import selected
            </Button>
          )}
          {step === 2 && (
            <Button
              onClick={() => setStep(3)}
              disabled={
                testUserSetupPhase !== 'ready' ||
                isLoadingGroups ||
                isErrorGroups ||
                !groupsData?.groups?.length
              }
            >
              Select Groups
            </Button>
          )}
          {step === 3 && (
            <Button
              onClick={() => {
                if (selectedGroupIds.length === 0) {
                  setErrorMsg('At least one group must be selected.')
                  return
                }
                setErrorMsg(null)
                setStep(4)
              }}
            >
              Continue
            </Button>
          )}
          {step === 4 && (
            <Button onClick={handleSyncConfigs} disabled={syncMut.isPending}>
              {syncMut.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              {panelsRemaining > 1 ? 'Sync & next panel' : 'Sync Configs & Hosts'}
            </Button>
          )}
          {(step === 0 || step === 5) && (
            <Button variant="outline" onClick={() => onOpenChange(false)}>
              {step === 5 ? 'Done' : 'Cancel'}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
