import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { cn } from "@/lib/utils"

const badgeVariants = cva(
  "inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-semibold transition-colors focus:outline-none focus:ring-2 focus:ring-cyan-400 focus:ring-offset-2 focus:ring-offset-[#0c1424] backdrop-blur-md",
  {
    variants: {
      variant: {
        default:
          "border-transparent bg-cyan-500/20 text-cyan-300 shadow hover:bg-cyan-500/30",
        secondary:
          "border-white/10 bg-white/5 text-slate-200 hover:bg-white/10",
        destructive:
          "border-transparent bg-red-500/20 text-red-400 shadow hover:bg-red-500/30",
        outline: "text-slate-200 border-white/20",
        success: "border-transparent bg-emerald-500/20 text-emerald-400 shadow hover:bg-emerald-500/30",
        warning: "border-transparent bg-amber-500/20 text-amber-400 shadow hover:bg-amber-500/30",
        info: "border-transparent bg-blue-500/20 text-blue-400 shadow hover:bg-blue-500/30",
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
