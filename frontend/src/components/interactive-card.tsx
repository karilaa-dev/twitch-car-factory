import type * as React from "react"
import { useNavigate } from "react-router-dom"

import { Card } from "@/components/ui/card"
import { TableRow } from "@/components/ui/table"
import { cn } from "@/lib/utils"

const nestedControlSelector =
  "a, button, input, select, textarea, [role='button'], [role='checkbox'], [role='switch']"

function useOpenItem(to: string) {
  const navigate = useNavigate()

  return {
    role: "link" as const,
    tabIndex: 0,
    onClick: (event: React.MouseEvent<HTMLElement>) => {
      const control = (event.target as Element).closest(nestedControlSelector)
      if (!control || control === event.currentTarget) navigate(to)
    },
    onKeyDown: (event: React.KeyboardEvent<HTMLElement>) => {
      if (event.target !== event.currentTarget || event.key !== "Enter") return
      event.preventDefault()
      navigate(to)
    },
  }
}

export function InteractiveCard({
  to,
  className,
  ...props
}: React.ComponentProps<typeof Card> & { to: string }) {
  const activation = useOpenItem(to)
  return (
    <Card
      className={cn(
        "cursor-pointer transition-shadow hover:ring-primary/40 focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none",
        className
      )}
      {...activation}
      {...props}
    />
  )
}

export function InteractiveTableRow({
  to,
  className,
  ...props
}: React.ComponentProps<typeof TableRow> & { to: string }) {
  const activation = useOpenItem(to)
  return (
    <TableRow
      className={cn(
        "cursor-pointer focus-visible:bg-accent focus-visible:outline-none",
        className
      )}
      {...activation}
      {...props}
    />
  )
}
