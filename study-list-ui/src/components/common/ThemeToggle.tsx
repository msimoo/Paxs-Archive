import { Moon, Sun } from 'lucide-react';
import { cn } from '@/lib/utils';
import { useTheme } from '@/hooks/use-theme';

/**
 * The day/night switch. One control, two icons, and a knob that moves — the
 * same gesture the dashboard's icon button performs, but self-explanatory
 * without a tooltip.
 *
 * Exposed as `role="switch"` rather than two buttons: a screen reader should
 * hear "colour theme, on" once, not "light" and "dark" as siblings.
 */
export function ThemeToggle({ className }: { className?: string }) {
  const { theme, toggleTheme } = useTheme();
  const isLight = theme === 'light';

  return (
    <button
      type="button"
      role="switch"
      aria-checked={isLight}
      aria-label="Toggle color theme"
      title={isLight ? 'Switch to dark reading-room mode' : 'Switch to light clinical mode'}
      onClick={toggleTheme}
      className={cn(
        'focus-ring relative inline-flex h-8 w-[62px] shrink-0 items-center rounded-full border border-hairline-strong',
        'bg-raised-2/80 p-[3px] transition-colors duration-300 hover:border-accent/60',
        className,
      )}
    >
      <span
        aria-hidden="true"
        className={cn(
          'absolute top-[3px] left-[3px] h-[24px] w-[26px] rounded-full bg-accent shadow-[0_2px_10px_rgba(34,211,238,0.45)]',
          'transition-transform duration-300 ease-[cubic-bezier(0.22,0.61,0.36,1)]',
          isLight ? 'translate-x-[30px]' : 'translate-x-0',
        )}
      />
      <span className="relative z-10 flex w-full items-center justify-between px-[5px]">
        <Sun
          aria-hidden="true"
          className={cn(
            'h-[13px] w-[13px] transition-colors duration-300',
            isLight ? 'text-white' : 'text-faint',
          )}
        />
        <Moon
          aria-hidden="true"
          className={cn(
            'h-[13px] w-[13px] transition-colors duration-300',
            isLight ? 'text-faint' : 'text-[#04121f]',
          )}
        />
      </span>
    </button>
  );
}
