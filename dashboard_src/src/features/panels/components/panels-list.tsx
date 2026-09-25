import { useState, useMemo, useEffect, useCallback } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router'
import { AlertCircle, Plus, RefreshCw, Search, Server } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Card, CardContent } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import PanelCard from './panel-card'
import AddPanelWizardModal from '../dialogs/add-panel-wizard-modal'
import UpdatePanelModal from '../dialogs/update-panel-modal'
import { useGetPanels, type PanelItem } from '../service/panels-api'

export default function PanelsList() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const [searchTerm, setSearchTerm] = useState('')
  const [isAddModalOpen, setIsAddModalOpen] = useState(false)
  const [selectedPanelForUpdate, setSelectedPanelForUpdate] = useState<PanelItem | null>(null)

  const { data: panels = [], isLoading, isError, error, refetch, isFetching } = useGetPanels()

  // Listen to openNodeDialog / openAddPanelDialog custom events
  useEffect(() => {
    const handleOpenAdd = () => setIsAddModalOpen(true)
    window.addEventListener('openNodeDialog', handleOpenAdd)
    window.addEventListener('openAddPanelDialog', handleOpenAdd)
    return () => {
      window.removeEventListener('openNodeDialog', handleOpenAdd)
      window.removeEventListener('openAddPanelDialog', handleOpenAdd)
    }
  }, [])

  const filteredPanels = useMemo(() => {
    if (!searchTerm.trim()) return panels
    const q = searchTerm.toLowerCase().trim()
    return panels.filter(
      p =>
        p.name.toLowerCase().includes(q) ||
        p.purchaser_identity.toLowerCase().includes(q) ||
        p.source_panel_id.toLowerCase().includes(q) ||
        String(p.id).includes(q)
    )
  }, [panels, searchTerm])

  const handleManage = useCallback(
    (panel: PanelItem) => {
      navigate(`/panels/${panel.id}`)
    },
    [navigate]
  )

  const handleUpdate = useCallback((panel: PanelItem) => {
    setSelectedPanelForUpdate(panel)
  }, [])

  return (
    <div className="flex w-full flex-col p-4 sm:p-6 space-y-4">
      {/* Top action & filter bar */}
      <div className="flex flex-col sm:flex-row items-stretch sm:items-center justify-between gap-3">
        <div className="relative flex-1 max-w-md">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground rtl:left-auto rtl:right-3" />
          <Input
            placeholder="Search panels by name, purchaser, or ID..."
            value={searchTerm}
            onChange={e => setSearchTerm(e.target.value)}
            className="pl-9 rtl:pl-3 rtl:pr-9 h-9"
          />
        </div>

        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => refetch()}
            disabled={isFetching}
            className="h-9 gap-1.5"
            title="Refresh panels list"
          >
            <RefreshCw className={`h-4 w-4 ${isFetching ? 'animate-spin' : ''}`} />
            <span className="hidden sm:inline">{t('refresh', { defaultValue: 'Refresh' })}</span>
          </Button>

          <Button
            variant="default"
            size="sm"
            onClick={() => setIsAddModalOpen(true)}
            className="h-9 gap-1.5 font-medium"
          >
            <Plus className="h-4 w-4" />
            <span>{t('panels.addPanel', { defaultValue: 'Add Panel' })}</span>
          </Button>
        </div>
      </div>

      {/* Loading state */}
      {isLoading && (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
          {Array.from({ length: 6 }).map((_, i) => (
            <Card key={i} className="p-4 space-y-3">
              <div className="flex justify-between items-start">
                <div className="flex items-center gap-2.5">
                  <Skeleton className="h-9 w-9 rounded-lg" />
                  <div className="space-y-1.5">
                    <Skeleton className="h-4 w-28" />
                    <Skeleton className="h-3 w-16" />
                  </div>
                </div>
                <Skeleton className="h-5 w-20 rounded-full" />
              </div>
              <Skeleton className="h-14 w-full rounded-lg" />
              <div className="flex justify-end gap-2 pt-2">
                <Skeleton className="h-8 w-20" />
                <Skeleton className="h-8 w-20" />
              </div>
            </Card>
          ))}
        </div>
      )}

      {/* Error state */}
      {!isLoading && isError && (
        <Card className="border-destructive/30 bg-destructive/5 my-6">
          <CardContent className="flex flex-col items-center justify-center p-8 text-center space-y-3">
            <div className="flex h-12 w-12 items-center justify-center rounded-full bg-destructive/10 text-destructive">
              <AlertCircle className="h-6 w-6" />
            </div>
            <div className="space-y-1">
              <h3 className="text-base font-semibold text-foreground">
                {t('panels.loadError', { defaultValue: 'Unable to load panels.' })}
              </h3>
              <p className="text-sm text-muted-foreground max-w-sm">
                {(error as any)?.message || 'An error occurred while communicating with the integration API.'}
              </p>
            </div>
            <Button
              variant="outline"
              size="sm"
              onClick={() => refetch()}
              className="mt-2 gap-1.5 border-destructive/30 hover:bg-destructive/10"
            >
              <RefreshCw className="h-3.5 w-3.5" />
              <span>{t('panels.retry', { defaultValue: 'Retry' })}</span>
            </Button>
          </CardContent>
        </Card>
      )}

      {/* Empty state: No panels purchased/configured */}
      {!isLoading && !isError && panels.length === 0 && (
        <Card className="my-8">
          <CardContent className="flex flex-col items-center justify-center p-12 text-center space-y-4">
            <div className="flex h-16 w-16 items-center justify-center rounded-2xl bg-muted text-muted-foreground">
              <Server className="h-8 w-8 stroke-[1.5]" />
            </div>
            <div className="space-y-1.5 max-w-md">
              <h3 className="text-lg font-semibold text-foreground">
                {t('panels.noPanels', { defaultValue: 'No panels yet' })}
              </h3>
              <p className="text-sm text-muted-foreground">
                {t('panels.noPanelsDescription', {
                  defaultValue: 'Add a purchased panel to get started.',
                })}
              </p>
            </div>
            <Button
              variant="default"
              onClick={() => setIsAddModalOpen(true)}
              className="mt-2 gap-2"
            >
              <Plus className="h-4 w-4" />
              <span>{t('panels.addPanel', { defaultValue: 'Add Panel' })}</span>
            </Button>
          </CardContent>
        </Card>
      )}

      {/* Empty state: Search filter yielded 0 results */}
      {!isLoading && !isError && panels.length > 0 && filteredPanels.length === 0 && (
        <Card className="my-8">
          <CardContent className="flex flex-col items-center justify-center p-8 text-center space-y-2">
            <h3 className="text-base font-semibold">No panels match your search</h3>
            <p className="text-sm text-muted-foreground">Try clearing or adjusting your search term.</p>
            <Button variant="outline" size="sm" onClick={() => setSearchTerm('')} className="mt-2">
              Clear Search
            </Button>
          </CardContent>
        </Card>
      )}

      {/* Populated Panels Grid */}
      {!isLoading && !isError && filteredPanels.length > 0 && (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
          {filteredPanels.map(panel => (
            <PanelCard
              key={panel.id}
              panel={panel}
              onManage={handleManage}
              onUpdate={handleUpdate}
            />
          ))}
        </div>
      )}

      {/* Entry point modals */}
      <AddPanelWizardModal
        isOpen={isAddModalOpen}
        onOpenChange={setIsAddModalOpen}
      />

      <UpdatePanelModal
        panel={selectedPanelForUpdate}
        isOpen={Boolean(selectedPanelForUpdate)}
        onOpenChange={open => !open && setSelectedPanelForUpdate(null)}
      />
    </div>
  )
}
