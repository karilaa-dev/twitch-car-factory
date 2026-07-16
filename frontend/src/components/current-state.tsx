import { LoaderCircle } from "lucide-react"

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
  return observed !== desired && !brokenStates.has(observed)
}

export function CurrentState({
  observed,
  desired,
}: {
  observed: RuntimeStatus
  desired: "running" | "stopped"
}) {
  const showDesiredState = shouldShowDesiredState(observed, desired)

  return (
    <span className="inline-flex items-center gap-0.5">
      {showDesiredState ? (
        <Tooltip>
          <TooltipTrigger
            render={
              <Button
                variant="ghost"
                size="icon-xs"
                className="-mr-1 text-muted-foreground"
                aria-label={`Desired state: ${desired}`}
              />
            }
          >
            <LoaderCircle className="animate-spin" data-icon="inline-start" />
          </TooltipTrigger>
          <TooltipContent>
            <p>Desired state: {desired}</p>
          </TooltipContent>
        </Tooltip>
      ) : null}
      <StatusBadge status={observed} />
    </span>
  )
}
