import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { AlertTriangle } from "lucide-react"
import { toast } from "sonner"

import { PageSkeleton } from "@/components/page"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { useMediaQuery } from "@/hooks/use-media-query"
import { api, mutationError } from "@/lib/api"
import { DesktopOperationsGrid } from "@/pages/runtime-designs/operations-grid"
import { MobileStatusLanes } from "@/pages/runtime-designs/status-lanes"
import type { RuntimeSnapshot } from "@/types"

type RuntimeAction = "start" | "stop" | "restart"

export function RuntimePage() {
  const queryClient = useQueryClient()
  const desktop = useMediaQuery("(min-width: 48rem)")
  const snapshot = useQuery({
    queryKey: ["runtime"],
    queryFn: () => api<RuntimeSnapshot>("/runtime"),
    refetchInterval: 5_000,
  })
  const globalAction = useMutation({
    mutationFn: (value: RuntimeAction) =>
      api<{ action: string; queued: number }>("/runtime/actions", {
        method: "POST",
        json: { action: value },
      }),
    onSuccess: async (result) => {
      toast.success(
        `Queued ${result.action} for ${result.queued} account${result.queued === 1 ? "" : "s"}.`
      )
      await queryClient.invalidateQueries({ queryKey: ["runtime"] })
      await queryClient.invalidateQueries({ queryKey: ["accounts"] })
    },
    onError: mutationError,
  })
  const accountAction = useMutation({
    mutationFn: ({ id, value }: { id: number; value: RuntimeAction }) =>
      api(`/accounts/${id}/actions`, {
        method: "POST",
        json: { action: value },
      }),
    onSuccess: async () => {
      toast.success("Lifecycle command queued.")
      await queryClient.invalidateQueries({ queryKey: ["runtime"] })
    },
    onError: mutationError,
  })

  if (snapshot.isLoading) return <PageSkeleton />
  if (!snapshot.data) {
    return (
      <Alert variant="destructive">
        <AlertTriangle />
        <AlertTitle>Runtime unavailable</AlertTitle>
        <AlertDescription>
          The current snapshot could not be loaded.
        </AlertDescription>
      </Alert>
    )
  }

  const viewProps = {
    data: snapshot.data,
    globalPending: globalAction.isPending,
    accountPending: accountAction.isPending,
    onGlobalAction: (value: RuntimeAction) => globalAction.mutate(value),
    onAccountAction: (id: number, value: RuntimeAction) =>
      accountAction.mutate({ id, value }),
  }

  return desktop ? (
    <DesktopOperationsGrid {...viewProps} />
  ) : (
    <MobileStatusLanes {...viewProps} />
  )
}
