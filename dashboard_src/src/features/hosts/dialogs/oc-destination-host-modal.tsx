import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Form, FormControl, FormField, FormItem, FormLabel, FormMessage } from '@/components/ui/form'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { LoaderButton } from '@/components/ui/loader-button'
import { UseFormReturn } from 'react-hook-form'
import { useTranslation } from 'react-i18next'
import { Pencil } from 'lucide-react'
import type { HostFormValues } from '@/features/hosts/forms/host-form'

interface OcDestinationHostModalProps {
  isDialogOpen: boolean
  onOpenChange: (open: boolean) => void
  form: UseFormReturn<HostFormValues>
  onSubmit: (data: HostFormValues) => Promise<{ status: number }>
  isSubmitting?: boolean
}

export function OcDestinationHostModal({
  isDialogOpen,
  onOpenChange,
  form,
  onSubmit,
  isSubmitting = false,
}: OcDestinationHostModalProps) {
  const { t } = useTranslation()

  return (
    <Dialog open={isDialogOpen} onOpenChange={onOpenChange}>
      <DialogContent onOpenAutoFocus={e => e.preventDefault()}>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Pencil className="h-5 w-5" />
            <span>{t('hostsDialog.editOcDestinationHost', { defaultValue: 'Edit imported OC host' })}</span>
          </DialogTitle>
          <DialogDescription>
            {t('hostsDialog.ocDestinationHostHint', {
              defaultValue:
                'This host is defined by the Outbound Center destination subscription. Only the display name can be changed here.',
            })}
          </DialogDescription>
        </DialogHeader>
        <Form {...form}>
          <form
            onSubmit={form.handleSubmit(async values => {
              await onSubmit({
                ...values,
                remark: values.remark.trim(),
              })
            })}
            className="space-y-4"
          >
            <FormField
              control={form.control}
              name="remark"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>{t('hostsDialog.hostnameDisplayName', { defaultValue: 'Hostname / display name' })}</FormLabel>
                  <FormControl>
                    <Input {...field} isError={!!form.formState.errors.remark} />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />
            <div className="flex justify-end gap-2">
              <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
                {t('cancel')}
              </Button>
              <LoaderButton type="submit" isLoading={isSubmitting} loadingText={t('saving', { defaultValue: 'Saving…' })}>
                {t('save')}
              </LoaderButton>
            </div>
          </form>
        </Form>
      </DialogContent>
    </Dialog>
  )
}
