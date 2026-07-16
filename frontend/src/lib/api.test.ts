import { describe, expect, it, vi } from "vitest"

import { api } from "@/lib/api"

describe("api", () => {
  it("unwraps the success envelope", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ data: { ok: true }, notices: [] }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        })
      )
    )
    await expect(api<{ ok: boolean }>("/session")).resolves.toEqual({
      ok: true,
    })
  })

  it("maps field failures to ApiError", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            error: {
              code: "validation_error",
              message: "Invalid",
              fields: { username: ["Already managed."] },
            },
          }),
          { status: 400, headers: { "Content-Type": "application/json" } }
        )
      )
    )
    await expect(
      api("/accounts", { method: "POST", json: {} })
    ).rejects.toMatchObject({
      code: "validation_error",
      fields: { username: ["Already managed."] },
      status: 400,
    })
  })

  it("sends CSRF on mutations without leaking it into reads", async () => {
    document.cookie = "csrftoken=abc123"
    const fetchMock = vi.fn().mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify({ data: {}, notices: [] }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        })
      )
    )
    vi.stubGlobal("fetch", fetchMock)
    await api("/settings/general", { method: "PUT", json: {} })
    await api("/session")
    const mutationHeaders = fetchMock.mock.calls[0][1].headers as Headers
    const readHeaders = fetchMock.mock.calls[1][1].headers as Headers
    expect(mutationHeaders.get("X-CSRFToken")).toBe("abc123")
    expect(readHeaders.get("X-CSRFToken")).toBeNull()
  })

  it("accepts an empty 204 response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(null, { status: 204 }))
    )
    await expect(api("/session", { method: "DELETE" })).resolves.toBeUndefined()
  })

  it("normalizes non-JSON responses", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("Bad gateway", { status: 502 }))
    )
    await expect(api("/runtime")).rejects.toMatchObject({
      code: "invalid_response",
      status: 502,
    })
  })

  it("rejects malformed notices before dispatching notifications", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ data: {}, notices: [null] }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        })
      )
    )
    await expect(api("/runtime")).rejects.toMatchObject({
      code: "invalid_response",
      status: 200,
    })
  })
})
