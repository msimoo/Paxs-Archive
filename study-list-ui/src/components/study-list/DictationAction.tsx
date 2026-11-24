import { useEffect, useRef, useState } from 'react';
import * as Popover from '@radix-ui/react-popover';
import { FileText, Headphones, X } from 'lucide-react';

function Player({ studyUid }: { studyUid: string }) {
  const player = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!player.current) return;
    if (window.paxsMountDictationPlayer) {
      return window.paxsMountDictationPlayer(player.current, studyUid);
    }
    player.current.textContent = 'Dictation player unavailable. Reload the study list.';
  }, [studyUid]);
  return <div ref={player} className="min-w-0" />;
}

export function DictationAction({ studyUid, count }: { studyUid: string; count: number }) {
  const [open, setOpen] = useState(false);
  const label = count > 0
    ? `Play study dictation (${count} ${count === 1 ? 'recording' : 'recordings'})`
    : 'Study dictation (no recordings yet)';

  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <Popover.Trigger asChild>
        <button
          type="button"
          className="focus-ring relative grid h-8 w-8 shrink-0 place-items-center rounded-lg text-accent transition-colors hover:bg-raised-2"
          aria-label={label}
          title={label}
        >
          <Headphones aria-hidden="true" className="h-3.5 w-3.5" />
          {count > 0 && (
            <span
              aria-hidden="true"
              className="absolute right-0.5 top-0.5 flex h-3.5 min-w-[14px] items-center justify-center rounded-full bg-accent px-1 font-mono text-[8.5px] font-bold leading-none text-accent-ink"
            >
              {count > 9 ? '9+' : count}
            </span>
          )}
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          align="end"
          sideOffset={8}
          collisionPadding={12}
          aria-label="Study dictation"
          className="z-50 w-[min(25rem,calc(100vw-1.25rem))] overflow-hidden rounded-2xl border border-hairline-strong bg-panel text-sm text-foreground shadow-[var(--paxs-shadow)]"
        >
          <div className="flex items-center justify-between border-b border-hairline bg-raised/60 px-3.5 py-2.5">
            <div className="flex items-center gap-2">
              <span className="grid size-6 place-items-center rounded-lg border border-accent/30 bg-accent/15 text-accent">
                <Headphones className="size-3.5" aria-hidden="true" />
              </span>
              <span className="text-[13px] font-semibold tracking-tight">Study Dictation</span>
              {count > 0 && (
                <span className="rounded-full border border-accent/30 bg-accent/10 px-2 py-0.5 font-mono text-[10px] font-semibold text-accent">
                  {count}
                </span>
              )}
            </div>
            <Popover.Close
              aria-label="Close dictation player"
              className="focus-ring grid size-7 place-items-center rounded-lg text-muted transition-colors hover:bg-raised-2 hover:text-foreground"
            >
              <X className="size-4" aria-hidden="true" />
            </Popover.Close>
          </div>

          <div className="p-3">
            <Player studyUid={studyUid} />
          </div>

          <div className="flex items-center justify-between border-t border-hairline bg-raised/40 px-3.5 py-2">
            <span className="font-mono text-[10px] text-faint">
              Resumes inside Report Editor
            </span>
            <button
              type="button"
              className="focus-ring inline-flex items-center gap-1.5 rounded-lg border border-accent/35 bg-accent/15 px-3 py-1.5 text-[12px] font-semibold text-accent transition-colors hover:bg-accent/25"
              onClick={() => {
                setOpen(false);
                window.paxsOpenReportModal?.(studyUid);
              }}
            >
              <FileText className="size-3.5" aria-hidden="true" />
              Open Report Editor
            </button>
          </div>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
