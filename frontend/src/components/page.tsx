import type { ReactNode } from "react"

import { Skeleton } from "@/components/ui/skeleton"

export function PageHeader({ title, description, actions }: { title: string; description: string; actions?: ReactNode }) {
  return (
    <div className="flex w-full min-w-0 flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
      <div className="flex min-w-0 flex-col gap-1">
        <h1 className="font-heading text-2xl font-semibold tracking-tight">{title}</h1>
        <p className="text-sm text-muted-foreground">{description}</p>
      </div>
      {actions ? <div className="flex w-full min-w-0 max-w-full flex-wrap items-center gap-2 sm:w-auto">{actions}</div> : null}
    </div>
  )
}

export function PageSkeleton() {
  return (
    <div className="grid gap-4" aria-label="Loading">
      <Skeleton className="h-16" />
      <Skeleton className="h-72" />
    </div>
  )
}
