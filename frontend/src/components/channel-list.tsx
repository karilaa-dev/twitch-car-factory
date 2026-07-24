import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"
import type * as React from "react"

type ChannelListProps = {
  channels: string[]
  empty?: string
  limit?: number
  className?: string
  variant?: React.ComponentProps<typeof Badge>["variant"]
  getVariant?: (
    channel: string,
    index: number
  ) => React.ComponentProps<typeof Badge>["variant"]
  getChannelAriaLabel?: (channel: string, index: number) => string
  ariaLabel?: string
}

export function ChannelList({
  channels,
  empty = "No channels",
  limit,
  className,
  variant = "outline",
  getVariant,
  getChannelAriaLabel,
  ariaLabel = "Farming channels",
}: ChannelListProps) {
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
            aria-label={getChannelAriaLabel?.(channel, index)}
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

function normalizeChannel(channel: string) {
  return channel.trim().toLowerCase()
}

export function WatchedChannelList({
  watchingChannels,
  ariaLabel = "Farming channels; currently watched channels are identified",
  ...props
}: Omit<ChannelListProps, "variant" | "getVariant" | "getChannelAriaLabel"> & {
  watchingChannels: string[]
}) {
  const watched = new Set(watchingChannels.map(normalizeChannel))
  const isWatching = (channel: string) => watched.has(normalizeChannel(channel))

  return (
    <ChannelList
      {...props}
      ariaLabel={ariaLabel}
      getVariant={(channel) => (isWatching(channel) ? "success" : "outline")}
      getChannelAriaLabel={(channel) =>
        `${channel} (${isWatching(channel) ? "currently watched" : "not currently watched"})`
      }
    />
  )
}
