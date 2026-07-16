"use client"

import { Toggle as TogglePrimitive } from "@base-ui/react/toggle"
import { type VariantProps } from "class-variance-authority"

import { cnState } from "@/lib/utils"
import { toggleVariants } from "@/components/ui/toggle-variants"

type ToggleProps = Omit<
  TogglePrimitive.Props & VariantProps<typeof toggleVariants>,
  "className"
> & {
  className?: TogglePrimitive.Props["className"]
}

function Toggle({
  className,
  variant = "default",
  size = "default",
  ...props
}: ToggleProps) {
  return (
    <TogglePrimitive
      data-slot="toggle"
      className={cnState<TogglePrimitive.State>(
        toggleVariants({ variant, size }),
        className
      )}
      {...props}
    />
  )
}

export { Toggle }
