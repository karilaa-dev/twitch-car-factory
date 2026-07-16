import { toast } from "sonner"

import {
  ApiError,
  type ApiErrorBody,
  type ApiNotice,
  type ApiResponse,
} from "@/types"

const API_ROOT = "/api/v1"

function csrfToken(): string {
  const token = document.cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith("csrftoken="))
  return token ? decodeURIComponent(token.slice("csrftoken=".length)) : ""
}

function throwApiError(body: ApiErrorBody, status: number): never {
  const error = new ApiError(body, status)
  if (error.status === 401 && window.location.pathname !== "/login") {
    window.location.assign("/login")
  }
  throw error
}

function invalidResponse(status: number): never {
  return throwApiError(
    {
      code: "invalid_response",
      message: "The server returned an invalid response.",
      fields: {},
    },
    status
  )
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null
}

function isErrorResponse(value: unknown): value is { error: ApiErrorBody } {
  if (!isRecord(value) || !isRecord(value.error)) return false
  return (
    typeof value.error.code === "string" &&
    typeof value.error.message === "string" &&
    isRecord(value.error.fields)
  )
}

function isNotice(value: unknown): value is ApiNotice {
  return (
    isRecord(value) &&
    typeof value.level === "string" &&
    ["info", "success", "warning", "error"].includes(value.level) &&
    typeof value.message === "string"
  )
}

export async function api<T>(
  path: string,
  init: RequestInit & { json?: unknown } = {}
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
  if (response.status === 204) return undefined as T

  const text = await response.text()
  let decoded: unknown
  try {
    decoded = JSON.parse(text)
  } catch {
    return invalidResponse(response.status)
  }
  if (isErrorResponse(decoded)) {
    return throwApiError(decoded.error, response.status)
  }
  if (!response.ok) {
    return throwApiError(
      { code: "request_failed", message: "The request failed.", fields: {} },
      response.status
    )
  }
  if (
    !isRecord(decoded) ||
    !("data" in decoded) ||
    !Array.isArray(decoded.notices) ||
    !decoded.notices.every(isNotice)
  ) {
    return invalidResponse(response.status)
  }
  const payload = decoded as unknown as ApiResponse<T>
  for (const notice of payload.notices) {
    const notify =
      notice.level === "error"
        ? toast.error
        : notice.level === "warning"
          ? toast.warning
          : toast.success
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

const dateTimeFormatter = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
})

export function formatTime(value: string | null | undefined): string {
  if (!value) return "—"
  return dateTimeFormatter.format(new Date(value))
}
