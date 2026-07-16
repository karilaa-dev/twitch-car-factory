import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

type StatefulClassName<State> =
  string | ((state: State) => string | undefined) | undefined

export function cnState<State>(
  base: ClassValue,
  className: StatefulClassName<State>
) {
  if (typeof className === "function") {
    return (state: State) => cn(base, className(state))
  }
  return cn(base, className)
}
