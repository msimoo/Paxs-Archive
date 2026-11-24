import { useId, type ReactNode } from 'react';
import { cn } from '@/lib/utils';

/**
 * Gradient duotone icon set.
 *
 * Every glyph is one 24×24 line drawing painted by two gradient paint servers:
 * a saturated stroke and a translucent wash that fills the volume of the shape.
 * That is what makes the icons read as "lit instruments" instead of flat
 * monochrome strokes, and it is why the same glyph stays legible on the dark
 * reading room and on the light clinical theme.
 *
 * `useId()` gives each instance its own gradient ids. Without it two icons of
 * the same tone on one page would share `<defs>` and the second would silently
 * inherit the first one's stops — a bug that only shows up after a reorder.
 */

export type IconTone = 'aqua' | 'emerald' | 'amber' | 'rose' | 'violet' | 'sky';

const TONE_STOPS: Record<IconTone, readonly [string, string]> = {
  aqua: ['#22d3ee', '#2dd4bf'],
  emerald: ['#34d399', '#22d3ee'],
  amber: ['#fbbf24', '#fb923c'],
  rose: ['#fb7185', '#f472b6'],
  violet: ['#a78bfa', '#f472b6'],
  sky: ['#38bdf8', '#22d3ee'],
};

interface GlyphProps {
  tone: IconTone;
  className?: string;
  /** Receives the two paint servers so a glyph can stroke with one and fill with the other. */
  draw: (stroke: string, fill: string) => ReactNode;
}

function Glyph({ tone, className, draw }: GlyphProps) {
  const uid = useId().replace(/:/g, '');
  const [from, to] = TONE_STOPS[tone];
  const stroke = `url(#paxs-s-${uid})`;
  const fill = `url(#paxs-f-${uid})`;

  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
      focusable="false"
      className={cn('size-4 shrink-0', className)}
    >
      <defs>
        <linearGradient id={`paxs-s-${uid}`} x1="3" y1="2" x2="21" y2="22" gradientUnits="userSpaceOnUse">
          <stop stopColor={from} />
          <stop offset="1" stopColor={to} />
        </linearGradient>
        <linearGradient id={`paxs-f-${uid}`} x1="12" y1="3" x2="12" y2="21" gradientUnits="userSpaceOnUse">
          <stop stopColor={from} stopOpacity="0.32" />
          <stop offset="1" stopColor={to} stopOpacity="0.06" />
        </linearGradient>
      </defs>
      <g stroke={stroke} strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
        {draw(stroke, fill)}
      </g>
    </svg>
  );
}

/** Archive of studies — three stacked plates, the study-list mark. */
export function IconStudies({ className, tone = 'aqua' }: { className?: string; tone?: IconTone }) {
  return (
    <Glyph
      tone={tone}
      className={className}
      draw={(_s, fill) => (
        <>
          <path d="M12 3.1 3.7 7.1l8.3 4 8.3-4L12 3.1Z" fill={fill} />
          <path d="m4.4 11.3 7.6 3.7 7.6-3.7" />
          <path d="m4.4 15.4 7.6 3.7 7.6-3.7" />
        </>
      )}
    />
  );
}

/** Live capture / online studies — a trace inside a disc. */
export function IconOnline({ className, tone = 'emerald' }: { className?: string; tone?: IconTone }) {
  return (
    <Glyph
      tone={tone}
      className={className}
      draw={(_s, fill) => (
        <>
          <circle cx="12" cy="12" r="8.5" fill={fill} />
          <path d="M6.5 12.3h2.6l1.6-3.3 2.2 6.3 1.5-3h2.9" />
        </>
      )}
    />
  );
}

/** Archived / cold tier — a lidded vault. */
export function IconArchived({ className, tone = 'amber' }: { className?: string; tone?: IconTone }) {
  return (
    <Glyph
      tone={tone}
      className={className}
      draw={(_s, fill) => (
        <>
          <rect x="3.3" y="4.4" width="17.4" height="4.1" rx="1.4" fill={fill} />
          <path d="M4.9 8.5v8.1a2.3 2.3 0 0 0 2.3 2.3h9.6a2.3 2.3 0 0 0 2.3-2.3V8.5" />
          <path d="M9.9 12.5h4.2" />
        </>
      )}
    />
  );
}

/** STAT / critical unread — a warning pyramid. */
export function IconCritical({ className, tone = 'rose' }: { className?: string; tone?: IconTone }) {
  return (
    <Glyph
      tone={tone}
      className={className}
      draw={(stroke, fill) => (
        <>
          <path d="M12 3.9 20.8 19.3a1 1 0 0 1-.9 1.5H4.1a1 1 0 0 1-.9-1.5L12 3.9Z" fill={fill} />
          <path d="M12 9.3v4.5" />
          <circle cx="12" cy="17" r=".95" fill={stroke} stroke="none" />
        </>
      )}
    />
  );
}

/** Diagnostic report — a chart with a folded corner. */
export function IconReport({ className, tone = 'violet' }: { className?: string; tone?: IconTone }) {
  return (
    <Glyph
      tone={tone}
      className={className}
      draw={(_s, fill) => (
        <>
          <path d="M13.4 3.4H6.8a2 2 0 0 0-2 2v13.2a2 2 0 0 0 2 2h10.4a2 2 0 0 0 2-2V8.8l-5.8-5.4Z" fill={fill} />
          <path d="M13.2 3.6v4.2a1.6 1.6 0 0 0 1.6 1.6h4.3" />
          <path d="M8.3 13.2h7.4M8.3 16.5h4.6" />
        </>
      )}
    />
  );
}

/** DICOM node topology — three linked nodes. */
export function IconNodes({ className, tone = 'sky' }: { className?: string; tone?: IconTone }) {
  return (
    <Glyph
      tone={tone}
      className={className}
      draw={(_s, fill) => (
        <>
          <circle cx="6" cy="7" r="2.3" fill={fill} />
          <circle cx="18" cy="7" r="2.3" fill={fill} />
          <circle cx="12" cy="17.6" r="2.3" fill={fill} />
          <path d="M8.1 8.3 10.5 15.4M15.9 8.3 13.5 15.4M8.4 7h7.2" />
        </>
      )}
    />
  );
}

/** Modality worklist / schedule — a calendar grid. */
export function IconSchedule({ className, tone = 'amber' }: { className?: string; tone?: IconTone }) {
  return (
    <Glyph
      tone={tone}
      className={className}
      draw={(stroke, fill) => (
        <>
          <rect x="3.4" y="5.3" width="17.2" height="14.9" rx="2.4" fill={fill} />
          <path d="M3.4 10.1h17.2M8.2 3.4v3.5M15.8 3.4v3.5" />
          <circle cx="8.5" cy="14" r="1" fill={stroke} stroke="none" />
          <circle cx="12" cy="14" r="1" fill={stroke} stroke="none" />
          <circle cx="15.5" cy="14" r="1" fill={stroke} stroke="none" />
        </>
      )}
    />
  );
}

/** Upload to the archive — tray with a rising arrow. */
export function IconUpload({ className, tone = 'emerald' }: { className?: string; tone?: IconTone }) {
  return (
    <Glyph
      tone={tone}
      className={className}
      draw={(_s, fill) => (
        <>
          <circle cx="12" cy="9.4" r="6.6" fill={fill} />
          <path d="M12 16.4V4.4" />
          <path d="m8.1 8.2 3.9-3.9 3.9 3.9" />
          <path d="M4.6 15.6v2.3a2.5 2.5 0 0 0 2.5 2.5h9.8a2.5 2.5 0 0 0 2.5-2.5v-2.3" />
        </>
      )}
    />
  );
}

/** Modality filter — a two-rail mixer. */
export function IconModality({ className, tone = 'sky' }: { className?: string; tone?: IconTone }) {
  return (
    <Glyph
      tone={tone}
      className={className}
      draw={(_s, fill) => (
        <>
          <path d="M4 7.4h4.3M12.7 7.4h7.3M4 16.6h7.3M15.7 16.6h4.3" />
          <circle cx="10.4" cy="7.4" r="2" fill={fill} />
          <circle cx="13.6" cy="16.6" r="2" fill={fill} />
        </>
      )}
    />
  );
}

/** Search — magnifier over a scan. */
export function IconSearch({ className, tone = 'aqua' }: { className?: string; tone?: IconTone }) {
  return (
    <Glyph
      tone={tone}
      className={className}
      draw={(_s, fill) => (
        <>
          <circle cx="10.7" cy="10.7" r="6.4" fill={fill} />
          <path d="m15.5 15.5 4 4" />
        </>
      )}
    />
  );
}

/** Table view. */
export function IconTable({ className, tone = 'aqua' }: { className?: string; tone?: IconTone }) {
  return (
    <Glyph
      tone={tone}
      className={className}
      draw={(_s, fill) => (
        <>
          <rect x="3.4" y="4.6" width="17.2" height="14.8" rx="2.3" fill={fill} />
          <path d="M3.4 9.3h17.2M3.4 14.5h17.2M9.4 9.3v10.1" />
        </>
      )}
    />
  );
}

/** Grid view. */
export function IconGrid({ className, tone = 'aqua' }: { className?: string; tone?: IconTone }) {
  return (
    <Glyph
      tone={tone}
      className={className}
      draw={(_s, fill) => (
        <>
          <rect x="3.4" y="4.6" width="17.2" height="14.8" rx="2.3" fill={fill} />
          <path d="M12 4.6v14.8M3.4 12h17.2" />
        </>
      )}
    />
  );
}

/** Report delivered downstream (EMR / ORU). */
export function IconSend({ className, tone = 'sky' }: { className?: string; tone?: IconTone }) {
  return (
    <Glyph
      tone={tone}
      className={className}
      draw={(_s, fill) => (
        <>
          <path d="M20.4 3.6 3.6 10.1l6.1 2.4 2.4 6.1 8.3-15Z" fill={fill} />
          <path d="m9.9 12.4 4.3-4.3" />
        </>
      )}
    />
  );
}

/** HIS bridge — a hospital block. */
export function IconHospital({ className, tone = 'violet' }: { className?: string; tone?: IconTone }) {
  return (
    <Glyph
      tone={tone}
      className={className}
      draw={(_s, fill) => (
        <>
          <path d="M4.4 20.4V6.2a1.6 1.6 0 0 1 1.6-1.6h6.4a1.6 1.6 0 0 1 1.6 1.6v14.2" fill={fill} />
          <path d="M14 20.4V11h4a1.6 1.6 0 0 1 1.6 1.6v7.8M3 20.4h18" />
          <path d="M9.2 8.4v3.2M7.6 10h3.2" />
        </>
      )}
    />
  );
}

/** Tenant isolation — a verified shield. */
export function IconShield({ className, tone = 'aqua' }: { className?: string; tone?: IconTone }) {
  return (
    <Glyph
      tone={tone}
      className={className}
      draw={(_s, fill) => (
        <>
          <path d="M12 3.4 19.3 6v6.2c0 4-3 7.1-7.3 8.4-4.3-1.3-7.3-4.4-7.3-8.4V6L12 3.4Z" fill={fill} />
          <path d="m9.2 11.9 2.2 2.2 3.9-3.9" />
        </>
      )}
    />
  );
}

