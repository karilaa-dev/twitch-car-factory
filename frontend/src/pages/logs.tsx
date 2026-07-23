import * as React from "react"
import { useInfiniteQuery, useQuery } from "@tanstack/react-query"
import {
  AlertTriangle,
  Archive,
  ArrowDownToLine,
  Download,
  FileArchive,
  FileText,
  History,
  Radio,
  RefreshCw,
} from "lucide-react"

import { PageHeader, PageSkeleton } from "@/components/page"
import { StatusBadge } from "@/components/status-badge"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Empty,
  EmptyContent,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty"
import { ScrollArea } from "@/components/ui/scroll-area"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Spinner } from "@/components/ui/spinner"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { api, formatTime, mutationError } from "@/lib/api"
import type {
  AccountList,
  LogRunDetail,
  LogRunList,
  LogRunSummary,
  LogTail,
} from "@/types"

const LIVE_BUFFER_LINES = 2_000
const COMBINED_SOURCE = "combined"
const ALL_ACCOUNTS = "all"
type LogKind = "all" | "worker" | "library"

const LOG_KIND_LABELS: Record<LogKind, string> = {
  all: "All output",
  worker: "Worker",
  library: "Twitch library",
}

function classifyLogLine(line: string): Exclude<LogKind, "all"> {
  if (
    /\s(?:TRACE|DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+twitch_farm\.miner_output:/.test(
      line
    ) ||
    /\s(?:TRACE|DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+library\s+account=/.test(
      line
    )
  ) {
    return "library"
  }
  return "worker"
}

function parseLibraryLine(line: string) {
  const archived = line.match(
    /\s(?:TRACE|DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+library\s+account=(\S+)\s+run=\S+:\s*(.*)$/
  )
  if (archived)
    return { account: archived[1], payload: archived[2], combined: false }

  const combined = line.match(
    /\s(?:TRACE|DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+twitch_farm\.miner_output:\s+miner\[([^\]]+)\]\s*(.*)$/
  )
  if (combined)
    return { account: combined[1], payload: combined[2], combined: true }
  return null
}

function libraryPayload(line: string) {
  return parseLibraryLine(line)?.payload ?? line
}

function isReadableLibraryLine(line: string) {
  const payload = libraryPayload(line)

  // Older runs may contain the inherited Python root-handler copy as well as
  // the miner's compact console copy. Hide that duplicate and all DEBUG/TRACE
  // payloads while retaining compact INFO/WARNING/ERROR records and tracebacks.
  if (
    /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[,.]\d+)?\s+(?:TRACE|DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+\S+:/.test(
      payload
    )
  ) {
    return false
  }
  return !/^\d{2}\/\d{2}(?:\/\d{2})?\s+\d{2}:\d{2}:\d{2}\s+-\s+(?:TRACE|DEBUG)\s+-/.test(
    payload
  )
}

function filterLogLines(lines: string[], kind: LogKind) {
  const readable = lines.filter(
    (line) =>
      classifyLogLine(line) !== "library" || isReadableLibraryLine(line)
  )
  if (kind === "all") return readable
  return readable.filter((line) => classifyLogLine(line) === kind)
}

function formatLogLine(line: string) {
  const parsed = parseLibraryLine(line)
  if (!parsed) return line
  return parsed.combined
    ? `${parsed.account} · ${parsed.payload}`
    : parsed.payload
}

function visibleLogLines(lines: string[], kind: LogKind) {
  return filterLogLines(lines, kind).map(formatLogLine)
}

function accountSource(accountId: number) {
  return `account:${accountId}`
}

function sourceAccountId(source: string): number | null {
  if (!source.startsWith("account:")) return null
  const value = Number(source.slice("account:".length))
  return Number.isInteger(value) && value > 0 ? value : null
}

function withQuery(
  path: string,
  values: Record<string, string | number | null | undefined>
) {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(values)) {
    if (value !== null && value !== undefined && value !== "")
      query.set(key, String(value))
  }
  const encoded = query.toString()
  return encoded ? `${path}?${encoded}` : path
}

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`
}

function formatDuration(startedAt: string, endedAt: string | null) {
  if (!endedAt) return "Still running"
  const seconds = Math.max(
    0,
    (new Date(endedAt).getTime() - new Date(startedAt).getTime()) / 1000
  )
  if (seconds < 60) return `${Math.round(seconds)}s`
  if (seconds < 3600)
    return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  return `${hours}h ${minutes}m`
}

const compactDateTimeFormatter = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
})

const compactTimeFormatter = new Intl.DateTimeFormat(undefined, {
  hour: "numeric",
  minute: "2-digit",
})

function formatRunWindow(startedAt: string, endedAt: string | null) {
  const started = new Date(startedAt)
  if (!endedAt) return `${compactDateTimeFormatter.format(started)}–now`

  const ended = new Date(endedAt)
  const sameDay =
    started.getFullYear() === ended.getFullYear() &&
    started.getMonth() === ended.getMonth() &&
    started.getDate() === ended.getDate()

  return sameDay
    ? `${compactDateTimeFormatter.format(started)}–${compactTimeFormatter.format(ended)}`
    : `${compactDateTimeFormatter.format(started)}–${compactDateTimeFormatter.format(ended)}`
}

function runOutcome(run: LogRunSummary) {
  const reason = run.stop_reason.replaceAll("_", " ") || "completed"
  if (run.exit_signal !== null) return `${reason} · signal ${run.exit_signal}`
  if (run.exit_code !== null) return `${reason} · exit ${run.exit_code}`
  return reason
}

function mergeWithOverlap(older: string[], newer: string[]) {
  const maximum = Math.min(older.length, newer.length)
  for (let size = maximum; size > 0; size -= 1) {
    const olderSuffix = older.slice(-size)
    const newerPrefix = newer.slice(0, size)
    if (
      olderSuffix.length === newerPrefix.length &&
      olderSuffix.every((line, index) => line === newerPrefix[index])
    ) {
      return [...older, ...newer.slice(size)]
    }
  }
  return [...older, ...newer]
}

export function LogsPage() {
  const accounts = useQuery({
    queryKey: ["accounts", "log-sources"],
    queryFn: () => api<AccountList>("/accounts"),
    refetchInterval: 10_000,
  })
  const [liveSource, setLiveSource] = React.useState(COMBINED_SOURCE)
  const [logKind, setLogKind] = React.useState<LogKind>("all")

  if (accounts.isLoading) return <PageSkeleton />
  if (!accounts.data)
    return (
      <Alert variant="destructive">
        <AlertTriangle />
        <AlertTitle>Logs unavailable</AlertTitle>
        <AlertDescription>
          Account sources could not be loaded. Refresh before inspecting farmer
          output.
        </AlertDescription>
      </Alert>
    )

  return (
    <>
      <PageHeader
        title="Logs"
        description="Live farmer telemetry and compressed, per-account run archives."
      />
      <Tabs defaultValue="live">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <TabsList variant="line">
            <TabsTrigger value="live">
              <Radio /> Live
            </TabsTrigger>
            <TabsTrigger value="history">
              <History /> History
            </TabsTrigger>
          </TabsList>
          <LogKindFilter value={logKind} onValueChange={setLogKind} />
        </div>
        <TabsContent value="live">
          <LiveLogs
            key={liveSource}
            accounts={accounts.data}
            source={liveSource}
            onSourceChange={setLiveSource}
            logKind={logKind}
          />
        </TabsContent>
        <TabsContent value="history">
          <LogHistory accounts={accounts.data} logKind={logKind} />
        </TabsContent>
      </Tabs>
    </>
  )
}

function LogKindFilter({
  value,
  onValueChange,
}: {
  value: LogKind
  onValueChange: (value: LogKind) => void
}) {
  return (
    <ToggleGroup
      aria-label="Log source type"
      value={[value]}
      onValueChange={(values) => {
        const next = values[0] as LogKind | undefined
        if (next) onValueChange(next)
      }}
      variant="outline"
      className="grid w-full grid-cols-3 sm:w-auto"
    >
      {(Object.keys(LOG_KIND_LABELS) as LogKind[]).map((kind) => (
        <ToggleGroupItem
          key={kind}
          value={kind}
          aria-label={`Show ${LOG_KIND_LABELS[kind]} logs`}
          className="min-h-11 px-2 text-xs sm:min-h-8 sm:px-3"
        >
          {LOG_KIND_LABELS[kind]}
        </ToggleGroupItem>
      ))}
    </ToggleGroup>
  )
}

function SourceSelect({
  accounts,
  value,
  onValueChange,
  includeCombined = false,
  includeAll = false,
}: {
  accounts: AccountList
  value: string
  onValueChange: (value: string) => void
  includeCombined?: boolean
  includeAll?: boolean
}) {
  const items = Object.fromEntries([
    ...(includeCombined ? [[COMBINED_SOURCE, "All farm activity"]] : []),
    ...(includeAll ? [[ALL_ACCOUNTS, "All accounts"]] : []),
    ...accounts.accounts.map((account) => [
      accountSource(account.id),
      account.username,
    ]),
  ])
  return (
    <Select
      items={items}
      value={value}
      onValueChange={(next) => next && onValueChange(next)}
    >
      <SelectTrigger aria-label="Log account" className="w-full sm:w-64">
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        <SelectGroup>
          {includeCombined ? (
            <SelectItem value={COMBINED_SOURCE}>All farm activity</SelectItem>
          ) : null}
          {includeAll ? (
            <SelectItem value={ALL_ACCOUNTS}>All accounts</SelectItem>
          ) : null}
          {accounts.accounts.map((account) => (
            <SelectItem key={account.id} value={accountSource(account.id)}>
              <span>{account.username}</span>
              {!account.is_active ? (
                <span className="text-muted-foreground">archived</span>
              ) : null}
            </SelectItem>
          ))}
        </SelectGroup>
      </SelectContent>
    </Select>
  )
}

function LiveLogs({
  accounts,
  source,
  onSourceChange,
  logKind,
}: {
  accounts: AccountList
  source: string
  onSourceChange: (value: string) => void
  logKind: LogKind
}) {
  const accountId = sourceAccountId(source)
  const rootRef = React.useRef<HTMLDivElement>(null)
  const pinnedRef = React.useRef(true)
  const cursorRef = React.useRef<string | null>(null)
  const newestLinesRef = React.useRef<string[]>([])
  const viewingOlderRef = React.useRef(false)
  const olderStateInvalidatedRef = React.useRef(false)
  const [lines, setLines] = React.useState<string[]>([])
  const [olderState, setOlderState] = React.useState<{
    runId: number
    before: string | null
  } | null>(null)
  const [loadingOlder, setLoadingOlder] = React.useState(false)
  const visibleLines = React.useMemo(
    () => visibleLogLines(lines, logKind),
    [lines, logKind]
  )
  const hasLines = visibleLines.length > 0
  const live = useQuery({
    queryKey: ["logs", "live", source],
    queryFn: () =>
      api<LogTail>(
        withQuery("/logs", {
          account_id: accountId,
          cursor: cursorRef.current,
        })
      ),
    refetchInterval: 2_000,
  })

  const viewport = React.useCallback(
    () =>
      rootRef.current?.querySelector<HTMLElement>(
        "[data-slot='scroll-area-viewport']"
      ) ?? null,
    []
  )
  const scrollToBottom = React.useCallback(() => {
    const element = viewport()
    if (element) {
      element.scrollTop = element.scrollHeight
      pinnedRef.current = true
    }
  }, [viewport])

  React.useEffect(() => {
    const element = viewport()
    if (!element) return
    const onScroll = () => {
      pinnedRef.current =
        element.scrollHeight - element.scrollTop - element.clientHeight < 24
    }
    element.addEventListener("scroll", onScroll, { passive: true })
    return () => element.removeEventListener("scroll", onScroll)
  }, [hasLines, viewport])

  React.useEffect(() => {
    if (!live.data) return
    const firstBatch = cursorRef.current === null
    const replace = live.data.reset || firstBatch
    const newest = (
      replace
        ? live.data.lines
        : mergeWithOverlap(newestLinesRef.current, live.data.lines)
    ).slice(-LIVE_BUFFER_LINES)
    newestLinesRef.current = newest
    if (replace) {
      viewingOlderRef.current = false
      olderStateInvalidatedRef.current = true
      setLines(newest)
    } else if (!viewingOlderRef.current) {
      setLines(newest)
    }
    cursorRef.current = live.data.cursor
  }, [live.data])

  React.useLayoutEffect(() => {
    if (pinnedRef.current) scrollToBottom()
  }, [visibleLines, scrollToBottom])

  const loadOlder = async () => {
    const runId = live.data?.run_id
    const currentOlderState =
      !olderStateInvalidatedRef.current && olderState?.runId === runId
        ? olderState
        : null
    const continuingOlderView = currentOlderState !== null
    const olderBefore = currentOlderState?.before
    if (!runId || olderBefore === null) return
    setLoadingOlder(true)
    pinnedRef.current = false
    try {
      let page = await api<LogRunDetail>(
        withQuery(`/logs/runs/${runId}`, { before: olderBefore })
      )
      let loadedLines = page.lines
      if (olderBefore === undefined && page.before) {
        const olderPage = await api<LogRunDetail>(
          withQuery(`/logs/runs/${runId}`, { before: page.before })
        )
        loadedLines = mergeWithOverlap(olderPage.lines, page.lines)
        page = olderPage
      }
      viewingOlderRef.current = true
      setLines((current) => {
        if (!continuingOlderView) return loadedLines.slice(-LIVE_BUFFER_LINES)
        return mergeWithOverlap(loadedLines, current).slice(
          0,
          LIVE_BUFFER_LINES
        )
      })
      olderStateInvalidatedRef.current = false
      setOlderState({ runId, before: page.before })
    } catch (error) {
      mutationError(error)
    } finally {
      setLoadingOlder(false)
    }
  }

  const selectedAccount = accountId
    ? accounts.accounts.find((account) => account.id === accountId)
    : null
  const sourceLabel = selectedAccount?.username ?? "All farm activity"
  const jumpToNewest = () => {
    const wasViewingOlder = viewingOlderRef.current
    viewingOlderRef.current = false
    olderStateInvalidatedRef.current = false
    setOlderState(null)
    pinnedRef.current = true
    if (wasViewingOlder) {
      setLines(newestLinesRef.current)
      window.requestAnimationFrame(scrollToBottom)
    } else {
      scrollToBottom()
    }
  }

  return (
    <Card>
      <CardHeader className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 flex-1">
          <CardTitle className="flex items-center gap-2">
            <span className="relative flex size-2">
              <span className="absolute inline-flex size-full animate-ping rounded-full bg-primary opacity-60" />
              <span className="relative inline-flex size-2 rounded-full bg-primary" />
            </span>
            Live stream
          </CardTitle>
          <CardDescription>
            {sourceLabel} · {LOG_KIND_LABELS[logKind]} · two-second incremental
            tail · {visibleLines.length.toLocaleString()} shown /{" "}
            {lines.length.toLocaleString()} buffered
          </CardDescription>
        </div>
        <div className="flex w-full flex-wrap items-center gap-2 sm:w-auto sm:flex-nowrap">
          <SourceSelect
            accounts={accounts}
            value={source}
            onValueChange={onSourceChange}
            includeCombined
          />
          {live.data ? (
            <StatusBadge status={live.data.supervisor.status} />
          ) : null}
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label="Refresh live logs"
            onClick={() => live.refetch()}
          >
            <RefreshCw data-icon="inline-start" />
          </Button>
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label="Jump to newest log line"
            onClick={jumpToNewest}
          >
            <ArrowDownToLine data-icon="inline-start" />
          </Button>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {live.isError ? (
          <Alert variant="destructive">
            <AlertTriangle />
            <AlertTitle>Live stream interrupted</AlertTitle>
            <AlertDescription>
              The current tail could not be refreshed. Archived files remain
              untouched.
            </AlertDescription>
          </Alert>
        ) : null}
        {live.data?.reset ? (
          <Alert>
            <RefreshCw />
            <AlertTitle>Tail position refreshed</AlertTitle>
            <AlertDescription>
              Rotation replaced the live cursor, so the console resumed from the
              newest retained lines.
            </AlertDescription>
          </Alert>
        ) : null}
        {hasLines ? (
          <div ref={rootRef}>
            <ScrollArea className="h-[min(68dvh,48rem)] rounded-lg border bg-muted/30">
              <pre
                className="min-w-max p-3 font-mono text-xs leading-relaxed"
                aria-label="Live farmer log lines"
              >
                {visibleLines.join("\n")}
              </pre>
            </ScrollArea>
          </div>
        ) : (
          <Empty className="border">
            <EmptyHeader>
              <EmptyMedia variant="icon">
                <FileText />
              </EmptyMedia>
              <EmptyTitle>
                {lines.length
                  ? `No ${LOG_KIND_LABELS[logKind].toLowerCase()} lines in this buffer`
                  : accountId
                    ? "No active farmer log"
                    : "No live log lines"}
              </EmptyTitle>
              <EmptyDescription>
                {lines.length
                  ? "Choose another output type or wait for matching activity. Cursor polling continues in the background."
                  : accountId
                    ? "This account is stopped or its current run has not emitted output yet."
                    : "The worker-owned combined log is empty or unavailable."}
              </EmptyDescription>
            </EmptyHeader>
          </Empty>
        )}
        {accountId &&
        live.data?.run_id &&
        (olderState?.runId !== live.data.run_id ||
          olderState.before !== null) ? (
          <div className="flex justify-start">
            <Button
              variant="outline"
              onClick={loadOlder}
              disabled={loadingOlder}
            >
              {loadingOlder ? (
                <Spinner data-icon="inline-start" />
              ) : (
                <Archive data-icon="inline-start" />
              )}
              Load older lines from this run
            </Button>
          </div>
        ) : null}
      </CardContent>
    </Card>
  )
}

function LogHistory({
  accounts,
  logKind,
}: {
  accounts: AccountList
  logKind: LogKind
}) {
  const [filter, setFilter] = React.useState(ALL_ACCOUNTS)
  const [selectedRunId, setSelectedRunId] = React.useState<number | null>(null)
  const accountId = sourceAccountId(filter)
  const history = useInfiniteQuery({
    queryKey: ["logs", "history", filter],
    queryFn: ({ pageParam }) =>
      api<LogRunList>(
        withQuery("/logs/runs", {
          account_id: accountId,
          before: pageParam,
          limit: 25,
        })
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.next_before ?? undefined,
  })
  const runs = React.useMemo(
    () => history.data?.pages.flatMap((page) => page.runs) ?? [],
    [history.data]
  )
  const effectiveSelectedRunId =
    selectedRunId && runs.some((run) => run.run_id === selectedRunId)
      ? selectedRunId
      : (runs[0]?.run_id ?? null)

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(18rem,24rem)_minmax(0,1fr)]">
      <Card>
        <CardHeader>
          <CardTitle>Run archive</CardTitle>
          <CardDescription>
            Completed farmer runs, newest first. Each account retains up to 50
            MiB compressed.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <SourceSelect
            accounts={accounts}
            value={filter}
            onValueChange={setFilter}
            includeAll
          />
          {history.isError ? (
            <Alert variant="destructive">
              <AlertTriangle />
              <AlertTitle>History unavailable</AlertTitle>
              <AlertDescription>
                The retained run index could not be loaded.
              </AlertDescription>
            </Alert>
          ) : null}
          {history.isLoading ? (
            <div className="flex min-h-40 items-center justify-center">
              <Spinner />
            </div>
          ) : runs.length ? (
            <div className="flex flex-col gap-1">
              {runs.map((run) => (
                <RunArchiveButton
                  key={run.run_id}
                  run={run}
                  selected={effectiveSelectedRunId === run.run_id}
                  onSelect={() => setSelectedRunId(run.run_id)}
                />
              ))}
              {history.hasNextPage ? (
                <Button
                  variant="outline"
                  onClick={() => history.fetchNextPage()}
                  disabled={history.isFetchingNextPage}
                >
                  {history.isFetchingNextPage ? (
                    <Spinner data-icon="inline-start" />
                  ) : (
                    <Archive data-icon="inline-start" />
                  )}
                  Load more runs
                </Button>
              ) : null}
            </div>
          ) : (
            <Empty className="border">
              <EmptyHeader>
                <EmptyMedia variant="icon">
                  <FileArchive />
                </EmptyMedia>
                <EmptyTitle>No archived runs</EmptyTitle>
                <EmptyDescription>
                  Per-account history begins after the updated worker starts a
                  farmer.
                </EmptyDescription>
              </EmptyHeader>
            </Empty>
          )}
        </CardContent>
      </Card>
      <RunArchiveViewer runId={effectiveSelectedRunId} logKind={logKind} />
    </div>
  )
}

function RunArchiveButton({
  run,
  selected,
  onSelect,
}: {
  run: LogRunSummary
  selected: boolean
  onSelect: () => void
}) {
  return (
    <Button
      variant={selected ? "secondary" : "ghost"}
      className={`h-auto min-h-0 w-full justify-start overflow-hidden rounded-md px-2.5 py-1.5 text-left whitespace-normal md:h-auto ${
        selected ? "border-l-primary border-l-2" : "border-l-2"
      }`}
      onClick={onSelect}
      data-log-run-id={run.run_id}
    >
      <span className="grid min-w-0 flex-1 grid-cols-[minmax(0,1fr)_auto] gap-x-2 gap-y-0.5">
        <span className="flex min-w-0 items-baseline gap-1.5 overflow-hidden leading-4">
          <span className="max-w-[50%] shrink-0 truncate text-sm font-medium">
            {run.account.username}
          </span>
          <span aria-hidden="true" className="text-muted-foreground">
            ·
          </span>
          <span
            className="min-w-0 truncate text-[11px] text-muted-foreground"
            title={runOutcome(run)}
          >
            {runOutcome(run)}
          </span>
        </span>
        <span className="shrink-0 font-mono text-[11px] leading-4 text-muted-foreground">
          #{run.run_id}
        </span>
        <span className="col-span-2 flex min-w-0 items-center gap-1 overflow-hidden text-[11px] leading-4 text-muted-foreground">
          <span
            className="min-w-0 truncate"
            title={`${formatTime(run.started_at)}–${formatTime(run.ended_at)}`}
          >
            {formatRunWindow(run.started_at, run.ended_at)}
          </span>
          <span aria-hidden="true">·</span>
          <span className="shrink-0">
            {formatDuration(run.started_at, run.ended_at)}
          </span>
          <span aria-hidden="true">·</span>
          <span className="shrink-0">{formatBytes(run.compressed_bytes)}</span>
          <span aria-hidden="true">·</span>
          <span className="shrink-0">{run.compressed_parts}p</span>
          {run.truncated ? (
            <Badge
              variant="destructive"
              className="h-3.5 shrink-0 px-1 text-[9px]"
            >
              truncated
            </Badge>
          ) : null}
          {run.archive_state === "compression_pending" ? (
            <Badge
              variant="secondary"
              className="h-3.5 shrink-0 px-1 text-[9px]"
            >
              pending
            </Badge>
          ) : null}
        </span>
      </span>
    </Button>
  )
}

function RunArchiveViewer({
  runId,
  logKind,
}: {
  runId: number | null
  logKind: LogKind
}) {
  const detail = useInfiniteQuery({
    queryKey: ["logs", "run", runId],
    queryFn: ({ pageParam }) =>
      api<LogRunDetail>(
        withQuery(`/logs/runs/${runId}`, { before: pageParam })
      ),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.before ?? undefined,
    enabled: runId !== null,
  })
  const pages = React.useMemo(
    () => detail.data?.pages ?? [],
    [detail.data?.pages]
  )
  const run = pages[0]?.run
  const lines = React.useMemo(
    () => [...pages].reverse().flatMap((page) => page.lines),
    [pages]
  )
  const visibleLines = React.useMemo(
    () => visibleLogLines(lines, logKind),
    [lines, logKind]
  )

  if (runId === null)
    return (
      <Card>
        <CardContent>
          <Empty>
            <EmptyHeader>
              <EmptyMedia variant="icon">
                <FileText />
              </EmptyMedia>
              <EmptyTitle>Select a run</EmptyTitle>
              <EmptyDescription>
                Choose an archived run to inspect its retained lines.
              </EmptyDescription>
            </EmptyHeader>
          </Empty>
        </CardContent>
      </Card>
    )

  if (detail.isLoading) return <PageSkeleton />
  if (detail.isError || !run)
    return (
      <Alert variant="destructive">
        <AlertTriangle />
        <AlertTitle>Run log unavailable</AlertTitle>
        <AlertDescription>
          The selected archive could not be opened.
        </AlertDescription>
      </Alert>
    )

  return (
    <Card>
      <CardHeader className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 flex-1">
          <CardTitle>
            {run.account.username} · run #{run.run_id}
          </CardTitle>
          <CardDescription>
            {formatTime(run.started_at)}–{formatTime(run.ended_at)} ·{" "}
            {runOutcome(run)} · {formatBytes(run.compressed_bytes)} ·{" "}
            {LOG_KIND_LABELS[logKind]}
          </CardDescription>
        </div>
        <div className="flex flex-wrap items-center gap-2 sm:shrink-0">
          {run.truncated ? (
            <Badge variant="destructive">partial retention</Badge>
          ) : null}
          {run.downloadable ? (
            <Button
              variant="outline"
              render={
                <a
                  href={`/api/v1/logs/runs/${run.run_id}/download`}
                  download={`account-${run.account.id}-run-${run.run_id}.log.gz`}
                  aria-label="Download gzip"
                />
              }
            >
              <Download data-icon="inline-start" /> Download gzip
            </Button>
          ) : (
            <Badge variant="secondary">compression pending</Badge>
          )}
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {run.truncated ? (
          <Alert>
            <Archive />
            <AlertTitle>Earlier lines expired</AlertTitle>
            <AlertDescription>
              This run crossed the account retention boundary. The viewer and
              download contain the retained suffix.
            </AlertDescription>
          </Alert>
        ) : null}
        {detail.hasNextPage ? (
          <div className="flex justify-start">
            <Button
              variant="outline"
              onClick={() => detail.fetchNextPage()}
              disabled={detail.isFetchingNextPage}
            >
              {detail.isFetchingNextPage ? (
                <Spinner data-icon="inline-start" />
              ) : (
                <Archive data-icon="inline-start" />
              )}
              Load older lines
            </Button>
          </div>
        ) : null}
        {visibleLines.length ? (
          <ScrollArea className="h-[min(64dvh,44rem)] rounded-lg border bg-muted/30">
            <pre
              className="min-w-max p-3 font-mono text-xs leading-relaxed"
              aria-label="Archived farmer log lines"
            >
              {visibleLines.join("\n")}
            </pre>
          </ScrollArea>
        ) : (
          <Empty className="border">
            <EmptyHeader>
              <EmptyMedia variant="icon">
                <FileText />
              </EmptyMedia>
              <EmptyTitle>
                {lines.length
                  ? `No ${LOG_KIND_LABELS[logKind].toLowerCase()} lines in this archive`
                  : "Archive contains no lines"}
              </EmptyTitle>
              <EmptyDescription>
                {lines.length
                  ? "Choose another output type to inspect the retained run."
                  : "The run record exists, but no readable log lines remain."}
              </EmptyDescription>
            </EmptyHeader>
            <EmptyContent>
              <Badge variant="outline">run #{run.run_id}</Badge>
            </EmptyContent>
          </Empty>
        )}
      </CardContent>
    </Card>
  )
}
