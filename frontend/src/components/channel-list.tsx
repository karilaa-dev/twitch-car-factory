import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"
import type * as React from "react"

export function ChannelList({
  channels,
  empty = "No channels",
  limit,
  className,
  variant = "outline",
  getVariant,
  ariaLabel = "Farming channels",
}: {
  channels: string[]
  empty?: string
  limit?: number
  className?: string
  variant?: React.ComponentProps<typeof Badge>["variant"]
  getVariant?: (
    channel: string,
    index: number
  ) => React.ComponentProps<typeof Badge>["variant"]
  ariaLabel?: string
}) {
  if (!channels.length) {
    return (
      <span className="font-mono text-sm text-muted-foreground">{empty}</span>
    )
  }

  const visible = limit ? channels.slice(0, limit) : channels
  const remaining = channels.length - visible.length

  return (
    <ul
      className={cn("flex min-w-0 flex-wrap gap-1.5", className)}
      aria-label={ariaLabel}
    >
      {visible.map((channel, index) => (
        <li key={channel} className="max-w-full min-w-0">
          <Badge
            variant={getVariant?.(channel, index) ?? variant}
            className="max-w-full font-mono font-normal tracking-tight"
          >
            {channel}
          </Badge>
        </li>
      ))}
      {remaining > 0 ? (
        <li>
          <Badge variant="secondary">+{remaining}</Badge>
        </li>
      ) : null}
    </ul>
  )
}
