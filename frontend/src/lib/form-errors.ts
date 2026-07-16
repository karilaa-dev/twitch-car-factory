import type { FieldValues, Path, UseFormSetError } from "react-hook-form"

import { ApiError } from "@/types"

type FormErrorOptions<T extends FieldValues> = {
  fields: readonly Path<T>[]
  aliases?: Readonly<Record<string, Path<T>>>
}

export function applyApiFormErrors<T extends FieldValues>(
  error: unknown,
  setError: UseFormSetError<T>,
  { fields, aliases = {} }: FormErrorOptions<T>
) {
  if (!(error instanceof ApiError)) {
    setError("root", { message: "The operation could not be completed." })
    return
  }

  const renderedFields = new Set<string>(fields)

  for (const [serverField, messages] of Object.entries(error.fields)) {
    const mappedField = aliases[serverField] ?? serverField
    const field =
      serverField === "__all__" || !renderedFields.has(mappedField)
        ? "root"
        : (mappedField as Path<T>)

    setError(field, { message: messages[0] ?? error.message })
  }

  if (!Object.keys(error.fields).length) {
    setError("root", { message: error.message })
  }
}
