import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { cn } from "@/lib/utils"

const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-xs font-semibold transition-colors focus:outline-none focus:ring-2 focus:ring-accent focus:ring-offset-2 focus:ring-offset-ink backdrop-blur-md",
  {
    variants: {
      variant: {
        default:
          "border-accent/35 bg-accent/15 text-accent hover:bg-accent/25",
        secondary:
          "border-hairline bg-raised-2/70 text-foreground hover:bg-raised-2",
        destructive:
          "border-danger/40 bg-danger/15 text-danger hover:bg-danger/25",
        outline: "text-foreground border-hairline-strong",
        success:
          "border-ok/40 bg-ok/15 text-ok hover:bg-ok/25",
        warning:
          "border-warn/40 bg-warn/15 text-warn hover:bg-warn/25",
        info:
          "border-teal/40 bg-teal/15 text-teal hover:bg-teal/25",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  }
)

export interface BadgeProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return (
    <div className={cn(badgeVariants({ variant }), className)} {...props} />
  )
}

export { Badge, badgeVariants }
