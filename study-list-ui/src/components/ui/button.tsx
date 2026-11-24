import * as React from "react"
import { Slot } from "@radix-ui/react-slot"
import { cva, type VariantProps } from "class-variance-authority"
import { cn } from "@/lib/utils"

/**
 * Variants are expressed in theme tokens (`accent`, `raised`, `hairline`), never
 * in fixed slate/cyan values: one class list has to look right in the reading
 * room *and* in clinical daylight.
 */
const buttonVariants = cva(
  "inline-flex items-center justify-center whitespace-nowrap rounded-lg text-sm font-medium ring-offset-ink transition-all duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-45 active:translate-y-px",
  {
    variants: {
      variant: {
        default:
          "bg-accent text-accent-ink font-semibold shadow-[0_6px_18px_-6px_var(--paxs-accent)] hover:brightness-110",
        destructive:
          "bg-danger text-white hover:brightness-110 shadow-[0_6px_18px_-8px_var(--paxs-danger)]",
        outline:
          "border border-hairline-strong bg-panel/60 text-foreground hover:border-accent/50 hover:bg-raised/70",
        secondary:
          "bg-raised-2 text-foreground hover:bg-raised-2/70 border border-hairline",
        ghost: "text-muted hover:bg-raised/70 hover:text-foreground",
        link: "text-accent underline-offset-4 hover:underline",
      },
      size: {
        default: "h-10 px-4 py-2",
        sm: "h-9 rounded-md px-3 text-[13px]",
        lg: "h-11 rounded-lg px-8",
        icon: "h-10 w-10",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
)

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, ...props }, ref) => {
    const Comp = asChild ? Slot : "button"
    return (
      <Comp
        className={cn(buttonVariants({ variant, size, className }))}
        ref={ref}
        {...props}
      />
    )
  }
)
Button.displayName = "Button"

export { Button, buttonVariants }
