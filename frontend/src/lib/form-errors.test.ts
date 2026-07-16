import { describe, expect, it, vi } from "vitest"
import type { UseFormSetError } from "react-hook-form"

import { applyApiFormErrors } from "@/lib/form-errors"
import { ApiError } from "@/types"

type AccountForm = {
  channels: string[]
  preset_id: number | null
}

const options = {
  fields: ["channels", "preset_id"] as const,
  aliases: {
    custom_channels: "channels",
    preset: "preset_id",
  } as const,
}

describe("applyApiFormErrors", () => {
  it("maps Django channel and preset aliases to rendered fields", () => {
    const setError = vi.fn() as UseFormSetError<AccountForm>
    const error = new ApiError(
      {
        code: "validation_error",
        message: "Invalid account",
        fields: {
          custom_channels: ["Channel does not exist."],
          preset: ["Choose a valid preset."],
        },
      },
      400
    )

    applyApiFormErrors(error, setError, options)

    expect(setError).toHaveBeenCalledWith("channels", {
      message: "Channel does not exist.",
    })
    expect(setError).toHaveBeenCalledWith("preset_id", {
      message: "Choose a valid preset.",
    })
  })

  it("surfaces unknown server fields at the visible root error", () => {
    const setError = vi.fn() as UseFormSetError<AccountForm>
    const error = new ApiError(
      {
        code: "validation_error",
        message: "Invalid account",
        fields: {
          unexpected_field: ["Unexpected server validation error."],
        },
      },
      400
    )

    applyApiFormErrors(error, setError, options)

    expect(setError).toHaveBeenCalledWith("root", {
      message: "Unexpected server validation error.",
    })
  })
})
