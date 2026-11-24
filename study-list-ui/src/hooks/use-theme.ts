import { useCallback, useEffect, useState } from 'react';

/**
 * The theme contract, deliberately identical to the dashboard
 * (`app/static/index.html` + `app.js`):
 *
 *   · the value lives in `localStorage.paxs_theme` ("dark" | "light")
 *   · it is applied as `data-theme` on `<html>`
 *   · dark is the default, because a reading room is a dark room
 *
 * Sharing the key means flipping the theme on the dashboard also flips this
 * SPA, and a user never has to set the same preference twice.
 */
export type Theme = 'dark' | 'light';

export const THEME_KEY = 'paxs_theme';
/** Dispatched on `window` after a change so other mounted toggles re-read it. */
export const THEME_EVENT = 'paxs:themechange';

function normalise(value: string | null | undefined): Theme {
  return value === 'light' ? 'light' : 'dark';
}

function readStored(): Theme {
  try {
    return normalise(window.localStorage.getItem(THEME_KEY));
  } catch {
    // Private-mode browsers throw on access; the attribute is still correct.
    return 'dark';
  }
}

function readApplied(): Theme {
  return normalise(document.documentElement.dataset.theme);
}

/** Apply a theme to the document and persist it. Safe to call repeatedly. */
export function applyTheme(theme: Theme, options: { persist?: boolean } = {}): void {
  document.documentElement.dataset.theme = theme;
  if (options.persist !== false) {
    try {
      window.localStorage.setItem(THEME_KEY, theme);
    } catch {
      // Persistence is best-effort: the session keeps the chosen theme.
    }
  }
  window.dispatchEvent(new CustomEvent<Theme>(THEME_EVENT, { detail: theme }));
}

export function useTheme(): { theme: Theme; toggleTheme: () => void; setTheme: (t: Theme) => void } {
  // The pre-paint script in index.html already set the attribute, so the first
  // render reads it rather than guessing — that keeps SSR-less hydration honest
  // and avoids a flash when the stored value is "light".
  const [theme, setThemeState] = useState<Theme>(() =>
    typeof document === 'undefined' ? 'dark' : readApplied(),
  );

  useEffect(() => {
    const sync = () => setThemeState(readApplied());
    const onStorage = (event: StorageEvent) => {
      if (event.key === THEME_KEY) {
        applyTheme(normalise(event.newValue), { persist: false });
        sync();
      }
    };
    window.addEventListener('storage', onStorage);
    window.addEventListener(THEME_EVENT, sync);
    return () => {
      window.removeEventListener('storage', onStorage);
      window.removeEventListener(THEME_EVENT, sync);
    };
  }, []);

  const setTheme = useCallback((next: Theme) => {
    applyTheme(next);
    setThemeState(next);
  }, []);

  const toggleTheme = useCallback(() => {
    setTheme(readApplied() === 'dark' ? 'light' : 'dark');
  }, [setTheme]);

  return { theme, toggleTheme, setTheme };
}

/** Re-export for tests that need to reset the stored preference. */
export { readStored };
