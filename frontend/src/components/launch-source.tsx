import { ChannelList, WatchedChannelList } from "@/components/channel-list"
import { Badge } from "@/components/ui/badge"
import { Separator } from "@/components/ui/separator"
import { cn } from "@/lib/utils"
import type { ReactNode } from "react"
import type { ChannelSource } from "@/types"

function normalizedName(value: string) {
  return value.trim().toLocaleLowerCase()
}

function launchSourcesMatch(current: ChannelSource, planned: ChannelSource) {
  return (
    current.mode === planned.mode &&
    normalizedName(current.name) === normalizedName(planned.name) &&
    current.channels.length === planned.channels.length &&
    current.channels.every(
      (channel, index) => channel === planned.channels[index]
    )
  )
}

function sourceReference(source: ChannelSource) {
  return source.mode === "preset"
    ? { label: "Source preset", mono: false }
    : source.mode === "custom"
      ? { label: "Account override", mono: true }
      : { label: "Configuration", mono: false }
}

function unchangedChannelIndexes(current: string[], planned: string[]) {
  const lengths = Array.from({ length: current.length + 1 }, () =>
    Array<number>(planned.length + 1).fill(0)
  )

  for (
    let currentIndex = current.length - 1;
    currentIndex >= 0;
    currentIndex--
  ) {
    for (
      let plannedIndex = planned.length - 1;
      plannedIndex >= 0;
      plannedIndex--
    ) {
      lengths[currentIndex][plannedIndex] =
        current[currentIndex] === planned[plannedIndex]
          ? lengths[currentIndex + 1][plannedIndex + 1] + 1
          : Math.max(
              lengths[currentIndex + 1][plannedIndex],
              lengths[currentIndex][plannedIndex + 1]
            )
    }
  }

  const currentIndexes = new Set<number>()
  const plannedIndexes = new Set<number>()
  let currentIndex = 0
  let plannedIndex = 0

  while (currentIndex < current.length && plannedIndex < planned.length) {
    if (current[currentIndex] === planned[plannedIndex]) {
      currentIndexes.add(currentIndex)
      plannedIndexes.add(plannedIndex)
      currentIndex++
      plannedIndex++
    } else if (
      lengths[currentIndex + 1][plannedIndex] >=
      lengths[currentIndex][plannedIndex + 1]
    ) {
      currentIndex++
    } else {
      plannedIndex++
    }
  }

  return { currentIndexes, plannedIndexes }
}

function DiffValue({
  direction,
  children,
}: {
  direction: "current" | "next"
  children: ReactNode
}) {
  return (
    <div className="grid min-w-0 grid-cols-[auto_3rem_minmax(0,1fr)] items-start gap-1.5">
      <span
        className={cn(
          "font-mono text-sm font-medium",
          direction === "current" ? "text-destructive" : "text-success"
        )}
        aria-hidden="true"
      >
        {direction === "current" ? "−" : "+"}
      </span>
      <span className="pt-0.5 text-xs text-muted-foreground">
        {direction === "current" ? "Current" : "Next"}
      </span>
      <div className="min-w-0">{children}</div>
    </div>
  )
}

function SourceSummary({
  source,
  watchingChannels,
}: {
  source: ChannelSource
  watchingChannels: string[]
}) {
  const reference = sourceReference(source)

  return (
    <div className="flex min-w-0 flex-col gap-3">
      <dl className="grid gap-3 sm:grid-cols-2">
        <div className="flex flex-col gap-1">
          <dt className="text-xs text-muted-foreground">Source mode</dt>
          <dd>
            <Badge variant="secondary" className="capitalize">
              {source.mode}
            </Badge>
          </dd>
        </div>
        <div className="min-w-0">
          <dt className="text-xs text-muted-foreground">{reference.label}</dt>
          <dd
            className={cn(
              "truncate text-sm font-medium",
              reference.mono && "font-mono"
            )}
          >
            {source.name}
          </dd>
        </div>
      </dl>
      <div className="flex flex-col gap-1">
        <p className="text-xs text-muted-foreground">Farming channels</p>
        <WatchedChannelList
          channels={source.channels}
          watchingChannels={watchingChannels}
        />
      </div>
    </div>
  )
}

function SourceDiff({
  current,
  planned,
  watchingChannels,
}: {
  current: ChannelSource
  planned: ChannelSource
  watchingChannels: string[]
}) {
  const currentReference = sourceReference(current)
  const plannedReference = sourceReference(planned)
  const modeChanged = current.mode !== planned.mode
  const referenceChanged =
    modeChanged || normalizedName(current.name) !== normalizedName(planned.name)
  const channelsChanged =
    current.channels.length !== planned.channels.length ||
    current.channels.some(
      (channel, index) => channel !== planned.channels[index]
    )
  const { currentIndexes, plannedIndexes } = unchangedChannelIndexes(
    current.channels,
    planned.channels
  )
  const watched = new Set(
    watchingChannels.map((channel) => channel.trim().toLowerCase())
  )
  const isWatching = (channel: string) =>
    watched.has(channel.trim().toLowerCase())

  return (
    <div className="rounded-lg border p-3" aria-label="Launch source diff">
      <dl className="grid gap-4 sm:grid-cols-2">
        <div className="flex min-w-0 flex-col gap-1.5">
          <dt className="text-xs text-muted-foreground">Source mode</dt>
          <dd className="flex min-w-0 flex-col gap-1.5">
            {modeChanged ? (
              <>
                <DiffValue direction="current">
                  <Badge variant="destructive" className="capitalize">
                    {current.mode}
                  </Badge>
                </DiffValue>
                <DiffValue direction="next">
                  <Badge variant="success" className="capitalize">
                    {planned.mode}
                  </Badge>
                </DiffValue>
              </>
            ) : (
              <Badge variant="secondary" className="capitalize">
                {current.mode}
              </Badge>
            )}
          </dd>
        </div>
        <div className="flex min-w-0 flex-col gap-1.5">
          <dt className="text-xs text-muted-foreground">
            {referenceChanged ? "Source reference" : currentReference.label}
          </dt>
          <dd className="flex min-w-0 flex-col gap-1.5">
            {referenceChanged ? (
              <>
                <DiffValue direction="current">
                  <div className="flex min-w-0 items-baseline gap-2">
                    <span className="shrink-0 text-xs text-muted-foreground">
                      {currentReference.label}
                    </span>
                    <span
                      className={cn(
                        "truncate text-sm font-medium text-destructive",
                        currentReference.mono && "font-mono"
                      )}
                    >
                      {current.name}
                    </span>
                  </div>
                </DiffValue>
                <DiffValue direction="next">
                  <div className="flex min-w-0 items-baseline gap-2">
                    <span className="shrink-0 text-xs text-muted-foreground">
                      {plannedReference.label}
                    </span>
                    <span
                      className={cn(
                        "truncate text-sm font-medium text-success",
                        plannedReference.mono && "font-mono"
                      )}
                    >
                      {planned.name}
                    </span>
                  </div>
                </DiffValue>
              </>
            ) : (
              <span
                className={cn(
                  "truncate text-sm font-medium",
                  currentReference.mono && "font-mono"
                )}
              >
                {current.name}
              </span>
            )}
          </dd>
        </div>
      </dl>
      <Separator className="my-3" />
      <div className="flex min-w-0 flex-col gap-1.5">
        <p className="text-xs text-muted-foreground">Farming channels</p>
        {channelsChanged ? (
          <div className="flex min-w-0 flex-col gap-2">
            <DiffValue direction="current">
              <ChannelList
                channels={current.channels}
                ariaLabel="Current farming channels"
                getVariant={(channel, index) =>
                  isWatching(channel)
                    ? "success"
                    : currentIndexes.has(index)
                      ? "outline"
                      : "destructive"
                }
                getChannelAriaLabel={(channel) =>
                  `${channel} (${isWatching(channel) ? "currently watched" : "not currently watched"})`
                }
              />
            </DiffValue>
            <DiffValue direction="next">
              <ChannelList
                channels={planned.channels}
                ariaLabel="Next farming channels"
                getVariant={(_channel, index) =>
                  plannedIndexes.has(index) ? "outline" : "success"
                }
              />
            </DiffValue>
          </div>
        ) : (
          <ChannelList channels={current.channels} />
        )}
      </div>
    </div>
  )
}

export function LaunchSource({
  current,
  planned,
  watchingChannels = [],
}: {
  current: ChannelSource
  planned: ChannelSource
  watchingChannels?: string[]
}) {
  const matches = launchSourcesMatch(current, planned)

  return (
    <section
      className="flex flex-col gap-2"
      aria-labelledby="launch-source-heading"
    >
      <div>
        <h3 id="launch-source-heading" className="font-medium">
          {matches ? "Launch source" : "Launch source change"}
        </h3>
        <p className="text-xs text-muted-foreground">
          {matches
            ? "Current and next launch use the same ordered source."
            : "The next start or restart will replace the immutable current source."}
        </p>
      </div>
      {matches ? (
        <SourceSummary source={current} watchingChannels={watchingChannels} />
      ) : (
        <SourceDiff
          current={current}
          planned={planned}
          watchingChannels={watchingChannels}
        />
      )}
    </section>
  )
}
