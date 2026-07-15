import { describe, expect, it, vi } from "vitest"

import { api } from "@/lib/api"
import { ApiError } from "@/types"

describe("api", () => {
  it("unwraps the success envelope", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ data: { ok: true }, notices: [] }), { status: 200, headers: { "Content-Type": "application/json" } })))
    await expect(api<{ ok: boolean }>("/session")).resolves.toEqual({ ok: true })
  })

  it("maps field failures to ApiError", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: { code: "validation_error", message: "Invalid", fields: { username: ["Already managed."] } } }), { status: 400, headers: { "Content-Type": "application/json" } })))
    await expect(api("/accounts", { method: "POST", json: {} })).rejects.toMatchObject<ApiError>({ code: "validation_error", fields: { username: ["Already managed."] }, status: 400 })
  })

  it("sends CSRF on mutations without leaking it into reads", async () => {
    document.cookie = "csrftoken=abc123"
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ data: {}, notices: [] }), { status: 200, headers: { "Content-Type": "application/json" } }))
    vi.stubGlobal("fetch", fetchMock)
    await api("/settings/general", { method: "PUT", json: {} })
    const headers = fetchMock.mock.calls[0][1].headers as Headers
    expect(headers.get("X-CSRFToken")).toBe("abc123")
  })
})
