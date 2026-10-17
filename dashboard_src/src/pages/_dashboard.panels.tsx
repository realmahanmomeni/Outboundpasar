import PageHeader from '@/components/layout/page-header'
import PageTransition from '@/components/layout/page-transition'
import Panels from '@/features/panels/components/panels-list'

export default function PanelsPage() {
  return (
    <div className="flex min-h-0 w-full flex-1 flex-col items-start gap-0">
      <PageTransition isContentTransition={true}>
        <PageHeader
          title="panels.title"
          description="panels.description"
        />
      </PageTransition>
      <div className="flex min-h-0 w-full flex-1 flex-col">
        <PageTransition isContentTransition={true} className="flex min-h-0 flex-1 flex-col">
          <Panels />
        </PageTransition>
      </div>
    </div>
  )
}
