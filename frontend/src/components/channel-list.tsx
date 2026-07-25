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

type RuntimeChannelListProps = Pick<
  ChannelListProps,
  "channels" | "empty" | "className" | "ariaLabel"
> & {
  watchingChannels: string[]
  onlineChannels: string[]
  getInactiveVariant?: (
    channel: string,
    originalIndex: number
  ) => React.ComponentProps<typeof Badge>["variant"]
}

export function RuntimeChannelLegend() {
  return (
    <div
      className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground"
      role="group"
      aria-label="Live channel status legend"
    >
      <Badge variant="success">Currently watched</Badge>
      <Badge variant="warning">Online, not watched</Badge>
      <Badge variant="outline">Offline or unknown</Badge>
    </div>
  )
}

export function RuntimeChannelList({
  channels,
  watchingChannels,
  onlineChannels,
  getInactiveVariant,
  ariaLabel = "Farming channels ordered by live status: watched, online, then offline",
  ...props
}: RuntimeChannelListProps) {
  const watched = new Set(watchingChannels.map(normalizeChannel))
  const online = new Set(onlineChannels.map(normalizeChannel))
  const isWatching = (channel: string) => watched.has(normalizeChannel(channel))
  const isOnline = (channel: string) => online.has(normalizeChannel(channel))
  const indexedChannels = channels.map((channel, originalIndex) => ({
    channel,
    originalIndex,
  }))
  const ordered = [
    ...indexedChannels.filter(({ channel }) => isWatching(channel)),
    ...indexedChannels.filter(
      ({ channel }) => !isWatching(channel) && isOnline(channel)
    ),
    ...indexedChannels.filter(
      ({ channel }) => !isWatching(channel) && !isOnline(channel)
    ),
  ]

  return (
    <ChannelList
      {...props}
      channels={ordered.map(({ channel }) => channel)}
      ariaLabel={ariaLabel}
      getVariant={(channel, index) =>
        isWatching(channel)
          ? "success"
          : isOnline(channel)
            ? "warning"
            : (getInactiveVariant?.(
                channel,
                ordered[index]?.originalIndex ?? -1
              ) ?? "outline")
      }
      getChannelAriaLabel={(channel) =>
        `${channel} (${
          isWatching(channel)
            ? "currently watched"
            : isOnline(channel)
              ? "online, not currently watched"
              : "offline or status unavailable"
        })`
      }
    />
  )
}
