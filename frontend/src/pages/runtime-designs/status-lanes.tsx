import type { LucideIcon } from "lucide-react"
import {
  AlertTriangle,
  CirclePause,
  Clock3,
  Play,
  RefreshCw,
  Square,
  TriangleAlert,
  Wifi,
} from "lucide-react"

import { WatchedChannelList } from "@/components/channel-list"
import { ConfirmAction } from "@/components/confirm-action"
import { CurrentState } from "@/components/current-state"
import { InteractiveCard } from "@/components/interactive-card"
import { PageHeader } from "@/components/page"
import { StatusBadge } from "@/components/status-badge"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { ButtonGroup } from "@/components/ui/button-group"
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { formatTime } from "@/lib/api"
import { needsRuntimeAttention } from "@/lib/runtime-state"
import { cn } from "@/lib/utils"
import type { AccountSummary, RuntimeSnapshot } from "@/types"

type Action = "start" | "stop" | "restart"
type Tone = "attention" | "running" | "idle"

function belongsInRunning(account: AccountSummary) {
  return (
    account.desired === "running" ||
    ["running", "starting", "restarting"].includes(account.observed)
  )
}

export function MobileStatusLanes({
  data,
  globalPending,
  accountPending,
  onGlobalAction,
  onAccountAction,
}: {
  data: RuntimeSnapshot
  globalPending: boolean
  accountPending: boolean
  onGlobalAction: (value: Action) => void
  onAccountAction: (id: number, value: Action) => void
}) {
  const attention = data.accounts.filter(needsRuntimeAttention)
  const healthy = data.accounts.filter(
    (account) => !needsRuntimeAttention(account)
  )
  const running = healthy.filter(belongsInRunning)
  const idle = healthy.filter((account) => !belongsInRunning(account))
  const lanes: Array<{
    id: string
    title: string
    description: string
    empty: string
    tone: Tone
    icon: LucideIcon
    accounts: AccountSummary[]
  }> = [
    {
      id: "attention",
      title: "Needs attention",
      description: "Incidents, faults, and unresolved target states.",
      empty: "No accounts need intervention.",
      tone: "attention",
      icon: TriangleAlert,
      accounts: attention,
    },
    {
      id: "running",
      title: "Running",
      description: "Healthy or transitioning toward the running target.",
      empty: "No accounts are running.",
      tone: "running",
      icon: Wifi,
      accounts: running,
    },
    {
      id: "idle",
      title: "Stopped or idle",
      description: "Accounts with no active farming process.",
      empty: "No accounts are idle.",
      tone: "idle",
      icon: CirclePause,
      accounts: idle,
    },
  ]
  const populatedLanes = lanes.filter((lane) => lane.accounts.length > 0)

  return (
    <div className="grid gap-4">
      <PageHeader
        title="Runtime"
        description="Accounts grouped by the action they need from an operator."
        actions={
          <ButtonGroup className="grid w-full grid-cols-3 sm:flex sm:w-auto">
            <Button
              variant="outline"
              disabled={globalPending}
              onClick={() => onGlobalAction("start")}
            >
              <Play /> Start all
            </Button>
            <ConfirmAction
              trigger={
                <Button variant="outline" disabled={globalPending}>
                  <RefreshCw /> Restart
                </Button>
              }
              title="Restart every configured account?"
              description="Running accounts will restart with their current immutable launch configuration."
              confirmLabel="Restart all"
              onConfirm={() => onGlobalAction("restart")}
            />
            <ConfirmAction
              trigger={
                <Button variant="destructive" disabled={globalPending}>
                  <Square /> Stop all
                </Button>
              }
              title="Stop every configured account?"
              description="The target state for all eligible accounts will be changed to stopped."
              confirmLabel="Stop all"
              onConfirm={() => onGlobalAction("stop")}
            />
          </ButtonGroup>
        }
      />

      {data.supervisor.status !== "healthy" ? (
        <Alert variant="destructive">
          <AlertTriangle />
          <AlertTitle>{data.supervisor.label}</AlertTitle>
          <AlertDescription>
            Worker heartbeats are unavailable or stale. Durable target state
            will resume when the supervisor returns.
          </AlertDescription>
        </Alert>
      ) : null}

      <Card size="sm" className="gap-0 py-0">
        <div className="grid grid-cols-2 divide-x divide-y">
          {[
            ["Accounts", data.summary.total],
            ["Attention", attention.length],
            ["Running", running.length],
            ["Idle", idle.length],
            ["Open incidents", data.summary.open_incidents],
          ].map(([label, value]) => (
            <div key={label} className="px-3 py-2.5">
              <p className="text-[0.68rem] font-medium tracking-wide text-muted-foreground uppercase">
                {label}
              </p>
              <p
                className={cn(
                  "mt-0.5 font-mono text-xl font-semibold tabular-nums",
                  ["Attention", "Open incidents"].includes(String(label)) &&
                    Number(value) > 0 &&
                    "text-destructive"
                )}
              >
                {value}
              </p>
            </div>
          ))}
        </div>
        <div className="flex items-center justify-between border-t bg-muted/20 px-3 py-2 text-xs text-muted-foreground">
          <span className="flex items-center gap-2">
            Supervisor <StatusBadge status={data.supervisor.status} />
          </span>
          <span className="font-mono">{formatTime(data.generated_at)}</span>
        </div>
      </Card>

      <section
        className="grid items-start gap-3"
        aria-label="Accounts by operating state"
      >
        {populatedLanes.map(
          ({ id, title, description, empty, tone, icon: Icon, accounts }) => (
            <section
              key={id}
              aria-labelledby={`${id}-title`}
              className={cn(
                "min-w-0 rounded-xl p-2.5 ring-1",
                tone === "attention" &&
                  "bg-destructive/[0.035] ring-destructive/25",
                tone === "running" && "bg-success/[0.025] ring-success/20",
                tone === "idle" && "bg-muted/20 ring-foreground/10"
              )}
            >
              <header className="flex min-h-14 items-start gap-2 px-1 py-1">
                <Icon
                  className={cn(
                    "mt-1 size-4 shrink-0",
                    tone === "attention" && "text-destructive",
                    tone === "running" && "text-success",
                    tone === "idle" && "text-muted-foreground"
                  )}
                />
                <div>
                  <div className="flex items-center gap-2">
                    <h2 id={`${id}-title`} className="font-heading font-medium">
                      {title}
                    </h2>
                    <Badge
                      variant={
                        tone === "attention" && accounts.length
                          ? "destructive"
                          : tone === "running"
                            ? "success"
                            : "secondary"
                      }
                      className="font-mono tabular-nums"
                    >
                      {accounts.length}
                    </Badge>
                  </div>
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    {description}
                  </p>
                </div>
              </header>

              <div className="grid gap-2.5">
                {accounts.length ? (
                  accounts.map((account) => {
                    const canLaunch =
                      account.is_active && account.has_credentials
                    const shouldStop = account.desired === "running"
                    return (
                      <InteractiveCard
                        key={account.id}
                        size="sm"
                        to={`/accounts/${account.id}`}
                        aria-label={`Open ${account.username}`}
                        className="gap-0 bg-card py-0 shadow-sm"
                      >
                        <CardHeader className="border-b py-3">
                          <CardTitle className="truncate">
                            {account.username}
                          </CardTitle>
                          <CardDescription className="truncate font-mono text-xs">
                            {account.config_key}
                          </CardDescription>
                          <CardAction>
                            <CurrentState
                              observed={account.observed}
                              desired={account.desired}
                            />
                          </CardAction>
                        </CardHeader>
                        <CardContent className="grid gap-3 py-3">
                          {account.open_incident ? (
                            <div className="flex gap-2 rounded-md bg-destructive/10 p-2 text-xs text-destructive">
                              <TriangleAlert className="mt-0.5 size-3.5 shrink-0" />
                              {account.open_incident.summary}
                            </div>
                          ) : null}
                          <div className="grid gap-1.5">
                            <div className="flex items-start justify-between gap-2">
                              <div className="min-w-0">
                                <p className="text-[0.68rem] tracking-wide text-muted-foreground uppercase">
                                  Channel source
                                </p>
                                <p className="truncate font-medium">
                                  {account.source.label || account.source.name}
                                </p>
                              </div>
                              <Badge variant="outline" className="capitalize">
                                {account.source.mode}
                              </Badge>
                            </div>
                            <WatchedChannelList
                              channels={account.source.channels}
                              watchingChannels={account.watching_channels}
                              limit={3}
                            />
                          </div>
                          <div className="flex items-center justify-between gap-2 border-t pt-2 text-xs text-muted-foreground">
                            <span className="flex items-center gap-1.5">
                              <Clock3 className="size-3.5" /> Heartbeat
                            </span>
                            <time className="font-mono text-[0.68rem]">
                              {formatTime(account.last_heartbeat)}
                            </time>
                          </div>
                          <ButtonGroup className="grid w-full grid-cols-2">
                            <Button
                              variant="outline"
                              disabled={
                                accountPending || (!shouldStop && !canLaunch)
                              }
                              onClick={() =>
                                onAccountAction(
                                  account.id,
                                  shouldStop ? "stop" : "start"
                                )
                              }
                            >
                              {shouldStop ? <Square /> : <Play />}
                              {shouldStop ? "Stop" : "Start"}
                            </Button>
                            <Button
                              variant="outline"
                              disabled={accountPending || !canLaunch}
                              onClick={() =>
                                onAccountAction(account.id, "restart")
                              }
                            >
                              <RefreshCw /> Restart
                            </Button>
                          </ButtonGroup>
                        </CardContent>
                      </InteractiveCard>
                    )
                  })
                ) : (
                  <div className="flex min-h-24 items-center justify-center rounded-lg border border-dashed px-4 text-center text-xs text-muted-foreground">
                    {empty}
                  </div>
                )}
              </div>
            </section>
          )
        )}
      </section>
    </div>
  )
}
