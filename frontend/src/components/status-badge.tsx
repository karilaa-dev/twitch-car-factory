import { Badge } from "@/components/ui/badge"
import type { RuntimeStatus } from "@/types"

const transitional = new Set<RuntimeStatus>(["starting", "stopping", "restarting", "queued", "leased"])
const destructive = new Set<RuntimeStatus>(["degraded", "failed", "open", "offline", "stale"])
const healthy = new Set<RuntimeStatus>(["running", "healthy", "succeeded", "recovered"])

export function StatusBadge({ status }: { status: RuntimeStatus | string }) {
  const value = status as RuntimeStatus
  const variant = destructive.has(value)
    ? "destructive"
    : healthy.has(value)
      ? "default"
      : transitional.has(value)
        ? "outline"
        : "secondary"
  return <Badge variant={variant}>{status.replaceAll("_", " ")}</Badge>
}
