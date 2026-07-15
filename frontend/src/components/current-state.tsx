import { Flag } from "lucide-react"

import { StatusBadge } from "@/components/status-badge"
import { Button } from "@/components/ui/button"
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip"
import type { RuntimeStatus } from "@/types"

const brokenStates = new Set<RuntimeStatus>([
  "degraded",
  "failed",
  "offline",
  "stale",
  "unknown",
])

function shouldShowDesiredState(
  observed: RuntimeStatus,
  desired: "running" | "stopped"
) {
  return (
    observed !== desired &&
    observed !== "stopped" &&
    !brokenStates.has(observed)
  )
}

export function CurrentState({
  observed,
  desired,
}: {
  observed: RuntimeStatus
  desired: "running" | "stopped"
}) {
  return (
    <span className="inline-flex items-center gap-0">
      <StatusBadge status={observed} />
      {shouldShowDesiredState(observed, desired) ? (
        <Tooltip>
          <TooltipTrigger
            render={
              <Button
                variant="ghost"
                size="icon-xs"
                className="-ml-3"
                aria-label={`Desired state: ${desired}`}
              />
            }
          >
            <Flag data-icon="inline-start" />
          </TooltipTrigger>
          <TooltipContent>
            <p>Desired state: {desired}</p>
          </TooltipContent>
        </Tooltip>
      ) : null}
    </span>
  )
}
