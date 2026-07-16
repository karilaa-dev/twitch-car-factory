import * as React from "react"
import { useQuery } from "@tanstack/react-query"
import { ArrowDownToLine, FileText, RefreshCw } from "lucide-react"

import { PageHeader, PageSkeleton } from "@/components/page"
import { StatusBadge } from "@/components/status-badge"
import { Button } from "@/components/ui/button"
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty"
import { ScrollArea } from "@/components/ui/scroll-area"
import { api, formatTime } from "@/lib/api"
import type { LogTail } from "@/types"

export function LogsPage() {
  const rootRef = React.useRef<HTMLDivElement>(null)
  const pinnedRef = React.useRef(true)
  const logs = useQuery({
    queryKey: ["logs"],
    queryFn: () => api<LogTail>("/logs"),
    refetchInterval: 5_000,
  })

  const viewport = () => rootRef.current?.querySelector<HTMLElement>("[data-slot='scroll-area-viewport']") ?? null
  const scrollToBottom = React.useCallback(() => {
    const element = viewport()
    if (element) {
      element.scrollTop = element.scrollHeight
      pinnedRef.current = true
    }
  }, [])

  React.useEffect(() => {
    const element = viewport()
    if (!element) return
    const onScroll = () => {
      pinnedRef.current = element.scrollHeight - element.scrollTop - element.clientHeight < 24
    }
    element.addEventListener("scroll", onScroll, { passive: true })
    return () => element.removeEventListener("scroll", onScroll)
  }, [logs.data])

  React.useLayoutEffect(() => {
    if (pinnedRef.current) scrollToBottom()
  }, [logs.data?.lines, scrollToBottom])

  if (logs.isLoading) return <PageSkeleton />
  return <>
    <PageHeader title="Logs" description="Bounded worker and web log tail, polled every five seconds." actions={<Button variant="outline" className="min-h-11 sm:min-h-8" onClick={() => logs.refetch()}><RefreshCw /> Refresh</Button>} />
    <Card>
      <CardHeader>
        <CardTitle>Runtime log</CardTitle>
        <CardDescription>{logs.data ? `${logs.data.line_count} of at most ${logs.data.max_lines} lines · refreshed ${formatTime(logs.data.generated_at)}` : "Log unavailable"}</CardDescription>
        <CardAction className="flex items-center gap-2">{logs.data ? <StatusBadge status={logs.data.supervisor.status} /> : null}<Button variant="ghost" size="icon-sm" aria-label="Jump to newest log line" onClick={scrollToBottom}><ArrowDownToLine /></Button></CardAction>
      </CardHeader>
      <CardContent>
        {logs.data?.lines.length ? (
          <div ref={rootRef}>
            <ScrollArea className="h-[min(68dvh,48rem)] rounded-lg border bg-muted/30">
              <pre className="min-w-max p-3 font-mono text-xs leading-relaxed" aria-label="Runtime log lines">{logs.data.lines.join("\n")}</pre>
            </ScrollArea>
          </div>
        ) : <Empty className="border"><EmptyHeader><EmptyMedia variant="icon"><FileText /></EmptyMedia><EmptyTitle>No log lines available</EmptyTitle><EmptyDescription>The bounded runtime log is empty or cannot be read.</EmptyDescription></EmptyHeader></Empty>}
      </CardContent>
    </Card>
  </>
}
