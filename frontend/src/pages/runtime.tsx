import * as React from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  useReactTable,
} from "@tanstack/react-table"
import { AlertTriangle, CircleOff, Play, RefreshCw, Square } from "lucide-react"
import { toast } from "sonner"

import { ChannelList } from "@/components/channel-list"
import { ConfirmAction } from "@/components/confirm-action"
import { CurrentState } from "@/components/current-state"
import {
  InteractiveCard,
  InteractiveTableRow,
} from "@/components/interactive-card"
import { PageHeader, PageSkeleton } from "@/components/page"
import { StatusBadge } from "@/components/status-badge"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
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
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty"
import { ScrollArea } from "@/components/ui/scroll-area"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { api, formatTime, mutationError } from "@/lib/api"
import type { AccountSummary, RuntimeSnapshot } from "@/types"

const column = createColumnHelper<AccountSummary>()

export function RuntimePage() {
  const queryClient = useQueryClient()
  const snapshot = useQuery({
    queryKey: ["runtime"],
    queryFn: () => api<RuntimeSnapshot>("/runtime"),
    refetchInterval: 5_000,
  })
  const action = useMutation({
    mutationFn: (value: "start" | "stop" | "restart") =>
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
    mutationFn: ({
      id,
      value,
    }: {
      id: number
      value: "start" | "stop" | "restart"
    }) =>
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

  const columns = React.useMemo(
    () => [
      column.accessor("config_key", {
        header: "Account",
        cell: ({ row }) => (
          <div className="grid gap-0.5">
            <span className="font-medium">{row.original.username}</span>
            <span className="font-mono text-xs text-muted-foreground">
              {row.original.config_key}
            </span>
          </div>
        ),
      }),
      column.accessor("observed", {
        header: "Current state",
        cell: ({ row }) => (
          <CurrentState
            observed={row.original.observed}
            desired={row.original.desired}
          />
        ),
      }),
      column.accessor("source.label", {
        header: "Channel source",
        cell: ({ row }) => (
          <div className="grid max-w-72 gap-0.5">
            <span>{row.original.source.label}</span>
            <ChannelList channels={row.original.source.channels} limit={3} />
          </div>
        ),
      }),
      column.accessor("last_heartbeat", {
        header: "Heartbeat",
        cell: (info) => formatTime(info.getValue()),
      }),
      column.display({
        id: "actions",
        header: () => <span className="sr-only">Actions</span>,
        cell: ({ row }) => (
          <ButtonGroup className="justify-end">
            {row.original.desired === "running" ? (
              <Button
                variant="outline"
                size="sm"
                aria-label={`Stop ${row.original.username}`}
                onClick={() =>
                  accountAction.mutate({ id: row.original.id, value: "stop" })
                }
              >
                <Square /> Stop
              </Button>
            ) : (
              <Button
                variant="outline"
                size="sm"
                aria-label={`Start ${row.original.username}`}
                disabled={
                  !row.original.is_active || !row.original.has_credentials
                }
                onClick={() =>
                  accountAction.mutate({ id: row.original.id, value: "start" })
                }
              >
                <Play /> Start
              </Button>
            )}
            <Button
              variant="outline"
              size="icon-sm"
              aria-label={`Restart ${row.original.username}`}
              disabled={
                !row.original.is_active || !row.original.has_credentials
              }
              onClick={() =>
                accountAction.mutate({ id: row.original.id, value: "restart" })
              }
            >
              <RefreshCw />
            </Button>
          </ButtonGroup>
        ),
      }),
    ],
    [accountAction]
  )
  const table = useReactTable({
    data: snapshot.data?.accounts ?? [],
    columns,
    getCoreRowModel: getCoreRowModel(),
  })

  if (snapshot.isLoading) return <PageSkeleton />
  if (!snapshot.data)
    return (
      <Alert variant="destructive">
        <AlertTriangle />
        <AlertTitle>Runtime unavailable</AlertTitle>
        <AlertDescription>
          The current snapshot could not be loaded.
        </AlertDescription>
      </Alert>
    )

  const data = snapshot.data
  return (
    <>
      <PageHeader
        title="Runtime"
        description="Current miner state from the shared supervisor."
        actions={
          <ButtonGroup className="grid w-full max-w-full grid-cols-3 *:w-full *:min-w-0 *:shrink sm:flex sm:w-fit sm:*:w-auto sm:*:shrink-0">
            <Button
              aria-label="Start all"
              variant="outline"
              className="min-h-11 overflow-hidden px-1 sm:min-h-8 sm:px-2.5"
              disabled={action.isPending}
              onClick={() => action.mutate("start")}
            >
              <Play /> Start<span className="global-control-suffix"> all</span>
            </Button>
            <ConfirmAction
              trigger={
                <Button
                  aria-label="Restart all"
                  variant="outline"
                  className="min-h-11 overflow-hidden px-1 sm:min-h-8 sm:px-2.5"
                  disabled={action.isPending}
                >
                  <RefreshCw /> Restart
                  <span className="global-control-suffix"> all</span>
                </Button>
              }
              title="Restart every configured account?"
              description="Running accounts will receive a coalesced restart command using their current immutable launch configuration."
              confirmLabel="Restart all"
              onConfirm={() => action.mutate("restart")}
            />
            <ConfirmAction
              trigger={
                <Button
                  aria-label="Stop all"
                  variant="destructive"
                  className="min-h-11 overflow-hidden px-1 sm:min-h-8 sm:px-2.5"
                  disabled={action.isPending}
                >
                  <Square /> Stop
                  <span className="global-control-suffix"> all</span>
                </Button>
              }
              title="Stop every configured account?"
              description="The target state for all eligible accounts will be changed to stopped."
              confirmLabel="Stop all"
              onConfirm={() => action.mutate("stop")}
            />
          </ButtonGroup>
        }
      />

      {data.supervisor.status !== "healthy" ? (
        <Alert variant="destructive">
          <AlertTriangle />
          <AlertTitle>{data.supervisor.label}</AlertTitle>
          <AlertDescription>
            Worker heartbeats are unavailable or stale. Target state remains
            durable and recovery will resume when the supervisor returns.
          </AlertDescription>
        </Alert>
      ) : null}

      <section
        className="grid grid-cols-2 gap-3 lg:grid-cols-4"
        aria-label="Runtime summary"
      >
        {[
          ["Accounts", data.summary.total],
          ["Running", data.summary.observed_running],
          ["Degraded", data.summary.degraded],
          ["Open incidents", data.summary.open_incidents],
        ].map(([label, value]) => (
          <Card key={label} size="sm">
            <CardHeader>
              <CardDescription>{label}</CardDescription>
              <CardTitle className="text-2xl tabular-nums">{value}</CardTitle>
            </CardHeader>
          </Card>
        ))}
      </section>

      <Card>
        <CardHeader>
          <CardTitle>Accounts</CardTitle>
          <CardDescription>
            Polling every five seconds. Focused controls are not replaced.
          </CardDescription>
          <CardAction>
            <StatusBadge status={data.supervisor.status} />
          </CardAction>
        </CardHeader>
        <CardContent>
          {data.accounts.length ? (
            <>
              <div className="hidden overflow-x-auto md:block">
                <Table>
                  <TableHeader>
                    {table.getHeaderGroups().map((group) => (
                      <TableRow key={group.id}>
                        {group.headers.map((header) => (
                          <TableHead key={header.id}>
                            {header.isPlaceholder
                              ? null
                              : flexRender(
                                  header.column.columnDef.header,
                                  header.getContext()
                                )}
                          </TableHead>
                        ))}
                      </TableRow>
                    ))}
                  </TableHeader>
                  <TableBody>
                    {table.getRowModel().rows.map((row) => (
                      <InteractiveTableRow
                        key={row.id}
                        to={`/accounts/${row.original.id}`}
                        aria-label={`Open ${row.original.username}`}
                      >
                        {row.getVisibleCells().map((cell) => (
                          <TableCell key={cell.id}>
                            {flexRender(
                              cell.column.columnDef.cell,
                              cell.getContext()
                            )}
                          </TableCell>
                        ))}
                      </InteractiveTableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
              <div className="grid gap-3 md:hidden">
                {data.accounts.map((account) => (
                  <InteractiveCard
                    key={account.id}
                    size="sm"
                    to={`/accounts/${account.id}`}
                    aria-label={`Open ${account.username}`}
                  >
                    <CardHeader>
                      <CardTitle>{account.username}</CardTitle>
                      <CardDescription className="font-mono">
                        {account.config_key}
                      </CardDescription>
                      <CardAction>
                        <CurrentState
                          observed={account.observed}
                          desired={account.desired}
                        />
                      </CardAction>
                    </CardHeader>
                    <CardContent className="grid gap-3">
                      <dl className="grid grid-cols-2 gap-2 text-sm">
                        <div className="col-span-2">
                          <dt className="text-muted-foreground">Source</dt>
                          <dd>{account.source.label}</dd>
                        </div>
                        <div className="col-span-2">
                          <dt className="mb-1 text-muted-foreground">
                            Channels
                          </dt>
                          <dd>
                            <ChannelList channels={account.source.channels} />
                          </dd>
                        </div>
                      </dl>
                      <ButtonGroup className="w-full">
                        <Button
                          variant="outline"
                          className="min-h-11 flex-1"
                          disabled={
                            !account.is_active || !account.has_credentials
                          }
                          onClick={() =>
                            accountAction.mutate({
                              id: account.id,
                              value:
                                account.desired === "running"
                                  ? "stop"
                                  : "start",
                            })
                          }
                        >
                          {account.desired === "running" ? (
                            <Square />
                          ) : (
                            <Play />
                          )}
                          {account.desired === "running" ? "Stop" : "Start"}
                        </Button>
                        <Button
                          variant="outline"
                          className="min-h-11 flex-1"
                          disabled={
                            !account.is_active || !account.has_credentials
                          }
                          onClick={() =>
                            accountAction.mutate({
                              id: account.id,
                              value: "restart",
                            })
                          }
                        >
                          <RefreshCw /> Restart
                        </Button>
                      </ButtonGroup>
                    </CardContent>
                  </InteractiveCard>
                ))}
              </div>
            </>
          ) : (
            <Empty>
              <EmptyHeader>
                <EmptyMedia variant="icon">
                  <CircleOff />
                </EmptyMedia>
                <EmptyTitle>No accounts configured</EmptyTitle>
                <EmptyDescription>
                  Add an account to begin supervising miners.
                </EmptyDescription>
              </EmptyHeader>
            </Empty>
          )}
        </CardContent>
      </Card>

      <Tabs defaultValue="incidents">
        <TabsList
          variant="line"
          className="w-full justify-start overflow-x-auto"
        >
          <TabsTrigger value="incidents">Incidents</TabsTrigger>
          <TabsTrigger value="commands">Command faults</TabsTrigger>
          <TabsTrigger value="activity">Operator activity</TabsTrigger>
        </TabsList>
        <TabsContent value="incidents">
          <EventCard
            title="Recent incidents"
            empty="No incidents recorded."
            items={data.incidents.map((item) => ({
              id: item.id,
              title: item.summary,
              meta: `${item.account_key ?? "Supervisor"} · ${formatTime(item.opened_at)}`,
              status: item.status,
              detail: item.details,
            }))}
          />
        </TabsContent>
        <TabsContent value="commands">
          <EventCard
            title="Failed commands"
            empty="No command faults recorded."
            items={data.command_faults.map((item) => ({
              id: item.id,
              title: `${item.action} · ${item.account_key}`,
              meta: `${item.actor} · ${formatTime(item.completed_at ?? item.created_at)}`,
              status: item.status,
              detail: item.error,
            }))}
          />
        </TabsContent>
        <TabsContent value="activity">
          <EventCard
            title="Operator activity"
            empty="No operator activity recorded."
            items={data.activity.map((item) => ({
              id: item.id,
              title: item.message || item.action,
              meta: `${item.actor} · ${formatTime(item.created_at)}`,
              detail: "",
            }))}
          />
        </TabsContent>
      </Tabs>
    </>
  )
}

function EventCard({
  title,
  empty,
  items,
}: {
  title: string
  empty: string
  items: Array<{
    id: number
    title: string
    meta: string
    status?: string
    detail: string
  }>
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardContent>
        {items.length ? (
          <ScrollArea className="max-h-96">
            <div className="grid gap-1">
              {items.map((item) => (
                <div
                  key={item.id}
                  className="grid gap-1 border-b py-3 last:border-0 sm:grid-cols-[1fr_auto]"
                >
                  <div className="min-w-0">
                    <p className="font-medium">{item.title}</p>
                    <p className="text-xs text-muted-foreground">{item.meta}</p>
                    {item.detail ? (
                      <p className="mt-1 text-sm text-muted-foreground">
                        {item.detail}
                      </p>
                    ) : null}
                  </div>
                  {item.status ? (
                    <div>
                      <StatusBadge status={item.status} />
                    </div>
                  ) : null}
                </div>
              ))}
            </div>
          </ScrollArea>
        ) : (
          <p className="py-8 text-center text-sm text-muted-foreground">
            {empty}
          </p>
        )}
      </CardContent>
    </Card>
  )
}
