import { toast } from "sonner"

import { ApiError, type ApiResponse } from "@/types"

const API_ROOT = "/api/v1"

function csrfToken(): string {
  const token = document.cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith("csrftoken="))
  return token ? decodeURIComponent(token.slice("csrftoken=".length)) : ""
}

export async function api<T>(
  path: string,
  init: RequestInit & { json?: unknown } = {},
): Promise<T> {
  const headers = new Headers(init.headers)
  let body = init.body
  if (init.json !== undefined) {
    headers.set("Content-Type", "application/json")
    body = JSON.stringify(init.json)
  }
  const method = (init.method ?? "GET").toUpperCase()
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    headers.set("X-CSRFToken", csrfToken())
  }
  headers.set("Accept", "application/json")

  const response = await fetch(`${API_ROOT}${path}`, {
    ...init,
    body,
    headers,
    credentials: "same-origin",
  })
  const payload = (await response.json()) as
    | ApiResponse<T>
    | { error: { code: string; message: string; fields: Record<string, string[]> } }
  if (!response.ok || "error" in payload) {
    const error =
      "error" in payload
        ? new ApiError(payload.error, response.status)
        : new ApiError(
            { code: "request_failed", message: "The request failed.", fields: {} },
            response.status,
          )
    if (error.status === 401 && window.location.pathname !== "/login") {
      window.location.assign("/login")
    }
    throw error
  }
  for (const notice of payload.notices) {
    const notify = notice.level === "error" ? toast.error : notice.level === "warning" ? toast.warning : toast.success
    notify(notice.message)
  }
  return payload.data
}

export function mutationError(error: unknown) {
  if (error instanceof ApiError) {
    const firstFieldError = Object.values(error.fields).flat()[0]
    toast.error(firstFieldError ?? error.message)
  } else {
    toast.error("The operation could not be completed.")
  }
}

export function formatTime(value: string | null | undefined): string {
  if (!value) return "—"
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value))
}
