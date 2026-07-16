import { AlertTriangle, Play, RefreshCw, Square } from "lucide-react"

import { ChannelList } from "@/components/channel-list"
import { ConfirmAction } from "@/components/confirm-action"
import { CurrentState } from "@/components/current-state"
import { InteractiveTableRow } from "@/components/interactive-card"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { ButtonGroup } from "@/components/ui/button-group"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { formatTime } from "@/lib/api"
import { needsRuntimeAttention } from "@/lib/runtime-state"
import { cn } from "@/lib/utils"
import type { AccountSummary, RuntimeSnapshot } from "@/types"

type Action = "start" | "stop" | "restart"

function AccountActions({
  account,
  pending,
  onAction,
}: {
  account: AccountSummary
  pending: boolean
  onAction: (id: number, value: Action) => void
}) {
  const stopping = account.desired === "running"
  const canLaunch = account.is_active && account.has_credentials
  return (
    <ButtonGroup>
      <Button
        variant="outline"
        size="sm"
        disabled={pending || (!stopping && !canLaunch)}
        aria-label={`${stopping ? "Stop" : "Start"} ${account.username}`}
        onClick={() => onAction(account.id, stopping ? "stop" : "start")}
      >
        {stopping ? <Square /> : <Play />}
        {stopping ? "Stop" : "Start"}
      </Button>
      <Button
        variant="outline"
        size="icon-sm"
        disabled={pending || !canLaunch}
        aria-label={`Restart ${account.username}`}
        onClick={() => onAction(account.id, "restart")}
      >
        <RefreshCw />
      </Button>
    </ButtonGroup>
  )
}

export function DesktopOperationsGrid({
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
  const accounts = data.accounts.toSorted(
    (a, b) =>
      Number(needsRuntimeAttention(b)) - Number(needsRuntimeAttention(a)) ||
      a.username.localeCompare(b.username)
  )
  const attention = accounts.filter(needsRuntimeAttention).length

  return (
    <div className="grid min-w-0 gap-4">
      <header className="flex flex-col gap-3 border-b pb-4 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <p className="mb-1 font-mono text-[0.68rem] font-semibold tracking-[0.18em] text-primary uppercase">
            Live operations
          </p>
          <h1 className="font-heading text-2xl font-semibold tracking-tight">
            Runtime
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Exceptions appear first · snapshot {formatTime(data.generated_at)}
          </p>
        </div>
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
                <RefreshCw /> Restart all
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
      </header>

      {data.supervisor.status !== "healthy" ? (
        <Alert variant="destructive">
          <AlertTriangle />
          <AlertTitle>{data.supervisor.label}</AlertTitle>
          <AlertDescription>
            Supervisor heartbeats are stale. Desired state remains durable.
          </AlertDescription>
        </Alert>
      ) : null}

      <Card size="sm" className="gap-0 py-0">
        <CardContent className="grid grid-cols-2 p-0 lg:grid-cols-5">
          {[
            ["Supervisor", data.supervisor.label],
            ["Accounts", data.summary.total],
            [
              "Live / target",
              `${data.summary.observed_running} / ${data.summary.desired_running}`,
            ],
            ["Need attention", attention],
            ["Open incidents", data.summary.open_incidents],
          ].map(([label, value]) => (
            <div key={label} className="border-r border-b p-3 lg:border-b-0">
              <p className="text-xs text-muted-foreground">{label}</p>
              <p
                className={cn(
                  "mt-0.5 font-mono text-lg font-semibold",
                  ["Need attention", "Open incidents"].includes(
                    String(label)
                  ) &&
                    Number(value) > 0 &&
                    "text-destructive"
                )}
              >
                {value}
              </p>
            </div>
          ))}
        </CardContent>
      </Card>

      <Card className="gap-0 py-0">
        <CardHeader className="border-b py-3">
          <CardTitle>Account operations</CardTitle>
          <CardDescription>
            {attention
              ? `${attention} exceptions pinned first`
              : "All accounts match their target state"}
          </CardDescription>
        </CardHeader>
        <div className="overflow-x-auto">
          <Table className="min-w-[880px]">
            <TableHeader className="bg-muted/40">
              <TableRow>
                <TableHead>Account</TableHead>
                <TableHead>Current state</TableHead>
                <TableHead>Launch source</TableHead>
                <TableHead>Heartbeat</TableHead>
                <TableHead className="text-right">Controls</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {accounts.map((account) => (
                <InteractiveTableRow
                  key={account.id}
                  to={`/accounts/${account.id}`}
                  aria-label={`Open ${account.username}`}
                  className={cn(
                    "h-16",
                    needsRuntimeAttention(account) && "bg-destructive/[0.04]"
                  )}
                >
                  <TableCell>
                    <div className="flex items-start gap-2">
                      {needsRuntimeAttention(account) ? (
                        <AlertTriangle className="mt-0.5 size-4 text-destructive" />
                      ) : (
                        <span className="mt-1.5 size-2 rounded-full bg-success" />
                      )}
                      <div>
                        <p className="font-medium">{account.username}</p>
                        <p className="font-mono text-xs text-muted-foreground">
                          {account.config_key}
                        </p>
                      </div>
                    </div>
                  </TableCell>
                  <TableCell>
                    <CurrentState
                      observed={account.observed}
                      desired={account.desired}
                    />
                  </TableCell>
                  <TableCell>
                    <p className="max-w-64 truncate font-medium">
                      {account.source.label || account.source.name}
                    </p>
                    <ChannelList channels={account.source.channels} limit={3} />
                  </TableCell>
                  <TableCell>
                    <p>{formatTime(account.last_heartbeat)}</p>
                    <p className="font-mono text-xs text-muted-foreground">
                      {account.pid ? `PID ${account.pid}` : "No process"}
                    </p>
                  </TableCell>
                  <TableCell>
                    <div className="flex justify-end">
                      <AccountActions
                        account={account}
                        pending={accountPending}
                        onAction={onAccountAction}
                      />
                    </div>
                  </TableCell>
                </InteractiveTableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </Card>
    </div>
  )
}
