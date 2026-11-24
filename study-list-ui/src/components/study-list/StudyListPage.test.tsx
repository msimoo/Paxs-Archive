import { afterEach, beforeEach, describe, expect, it, jest } from '@jest/globals';
import '@testing-library/jest-dom/jest-globals';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
jest.mock('./SearchToolbar', () => ({ SearchToolbar: () => null }));
jest.mock('./StatusChips', () => ({ StatusChips: () => null }));
jest.mock('./VitalsStrip', () => ({ VitalsStrip: () => null }));
jest.mock('../common/PaxsFooter', () => ({ PaxsFooter: () => null }));
jest.mock('./StudyDetailDrawer', () => ({ StudyDetailDrawer: () => null }));
jest.mock('./ShareModal', () => ({ ShareModal: () => null }));
jest.mock('./ForwardModal', () => ({ ForwardModal: () => null }));
import { StudyListPage } from './StudyListPage';
import { authNavigation, fetchStudies, openViewer } from '@/lib/api';
import type { Study, StudyFilters, StudyResponse } from '@/types/study';

const counts = {
  total: 1,
  online: 1,
  archived: 0,
  critical: 0,
  reported: 0,
  oru_sent: 0,
  his: 0,
};

const filters: StudyFilters = {
  page: 1,
  limit: 20,
  sort: 'date',
  order: 'desc',
  search: '',
  modality: '',
  state: '',
  critical: false,
  reported: false,
  oru: false,
  his: false,
  from: '',
  to: '',
  tenant: '',
};

const study: Study = {
  study_uid: '1.2.840.10008.1',
  patient_name: 'John^Doe',
  patient_id: 'PAT-1',
  patient_source: 'dicom',
  study_date: '2026-10-14',
  description: 'CT Chest',
  accession: 'ACC-1',
  modalities: ['CT'],
  n_series: 2,
  n_instances: 18,
  created_at: '2026-10-14T12:00:00Z',
  lifecycle_state: 'ONLINE',
};

function listResponse(items: Study[]): StudyResponse {
  return {
    items,
    total: items.length,
    page: 1,
    limit: 20,
    sort: 'date',
    order: 'desc',
    counts: { ...counts, total: items.length, online: items.length },
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  const text = status === 204 ? '' : JSON.stringify(body);
  return {
    status,
    ok: status >= 200 && status < 300,
    text: async () => text,
  } as Response;
}

class TestHeaders {
  private readonly values = new Map<string, string>();

  constructor(init?: HeadersInit) {
    if (init && !Array.isArray(init) && !(init instanceof Headers)) {
      for (const [name, value] of Object.entries(init)) this.set(name, String(value));
    }
  }

  has(name: string): boolean {
    return this.values.has(name.toLowerCase());
  }

  set(name: string, value: string): void {
    this.values.set(name.toLowerCase(), value);
  }

  get(name: string): string | null {
    return this.values.get(name.toLowerCase()) ?? null;
  }
}

const originalFetch = globalThis.fetch;
const originalHeaders = globalThis.Headers;

beforeEach(() => {
  localStorage.clear();
  document.documentElement.dataset.theme = 'dark';
  delete window.PAXS_BASE_PATH;
  window.history.replaceState({}, '', '/');
  Object.defineProperty(globalThis, 'Headers', { configurable: true, value: TestHeaders });
  if (typeof globalThis.fetch !== 'function') {
    globalThis.fetch = jest.fn(async () => jsonResponse({ detail: 'No test response' }, 500));
  }
});

afterEach(() => {
  cleanup();
  if (originalFetch) globalThis.fetch = originalFetch;
  else Reflect.deleteProperty(globalThis, 'fetch');
  if (originalHeaders) Object.defineProperty(globalThis, 'Headers', { configurable: true, value: originalHeaders });
  else Reflect.deleteProperty(globalThis, 'Headers');
  delete window.PAXS_BASE_PATH;
});

describe('study list regressions', () => {
  it('opens studies in the normal diagnostic view without probing for 3D', async () => {
    const openedTab = {
      opener: null,
      closed: false,
      document: { title: '', body: { appendChild: jest.fn() }, createElement: () => ({ textContent: '' }) },
      location: { replace: jest.fn() },
    } as unknown as Window;
    jest.spyOn(window, 'open').mockReturnValue(openedTab);
    const request = jest.spyOn(globalThis, 'fetch').mockImplementation(async () => ({
      ok: true,
      status: 200,
      json: async () => [{ '00080060': { Value: ['CT'] }, '0020000E': { Value: ['1.2.4'] } }],
    } as Response));

    openViewer('1.2.3');

    await waitFor(() => expect(openedTab.location.replace).toHaveBeenCalled());
    expect(openedTab.location.replace).toHaveBeenCalledWith(
      'http://localhost/viewer/viewer?StudyInstanceUIDs=1.2.3',
    );
    expect(request).not.toHaveBeenCalled();
  });

  it('uses the theme-matched brand logo and updates it when the theme changes', async () => {
    jest.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes('/auth/me')) return jsonResponse({ username: 'reader', role: 'RADIOLOGIST' });
      if (url.includes('/studies?')) return jsonResponse(listResponse([]));
      return jsonResponse({ detail: 'Unexpected test request' }, 404);
    });

    const user = userEvent.setup();
    render(<StudyListPage />);
    const logo = screen.getByRole('img', { name: 'PAXS Archive logo' });
    expect(logo).toHaveAttribute('src', '/static/logo/light-viewer.png');

    await user.click(screen.getByRole('switch', { name: 'Toggle color theme' }));
    expect(logo).toHaveAttribute('src', '/static/logo/dark-viewer.png');
    document.documentElement.dataset.theme = 'dark';
    localStorage.setItem('paxs_theme', 'dark');
  });

  it('removes a successfully deleted study before the refreshed list responds', async () => {
    let deleted = false;
    let releaseRefresh: (() => void) | undefined;
    jest.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.includes('/auth/me')) return jsonResponse({ username: 'boss', role: 'ADMIN' });
      if (url.includes('/studies?')) {
        if (!deleted) return jsonResponse(listResponse([study]));
        return new Promise<Response>((resolve) => {
          releaseRefresh = () => resolve(jsonResponse(listResponse([])));
        });
      }
      if (init?.method === 'DELETE') {
        deleted = true;
        return jsonResponse(null, 204);
      }
      return jsonResponse({ detail: 'Unexpected test request' }, 404);
    });

    const user = userEvent.setup();
    render(<StudyListPage />);
    expect(await screen.findByText('John Doe')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Delete study' }));
    await user.click(await screen.findByRole('button', { name: 'Confirm Delete' }));

    await waitFor(() => expect(screen.queryByText('John Doe')).not.toBeInTheDocument());
    expect(deleted).toBe(true);
    expect(releaseRefresh).toBeDefined();
    releaseRefresh?.();
  });

  it('does not offer the delete control to a non-admin', async () => {
    // The API admits ADMIN + RADIOLOGIST, but the product decision is that only
    // an administrator is *offered* the control. Before this gate every signed-in
    // account saw the button and got `Request failed (403)` on confirm.
    jest.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes('/auth/me')) return jsonResponse({ username: 'reader', role: 'RADIOLOGIST' });
      if (url.includes('/studies?')) return jsonResponse(listResponse([study]));
      return jsonResponse({ detail: 'Unexpected test request' }, 404);
    });

    render(<StudyListPage />);
    expect(await screen.findByText('John Doe')).toBeInTheDocument();

    // The row rendered (so the role request resolved) and no delete control exists.
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Delete study' })).not.toBeInTheDocument(),
    );
  });

  it('sends the CSRF header on a cookie-authenticated delete', async () => {
    // FS-10 parity with the dashboard SPA: the server requires X-CSRF-Token on
    // unsafe requests authenticated by the ambient session cookie. This client
    // never sent it, so delete/share/forward failed with
    // `403 CSRF token missing or invalid` in a tab without a stored access token.
    document.cookie = 'paxs_csrf=csrf-value-123';
    let deleteHeaders: TestHeaders | undefined;
    jest.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.includes('/auth/me')) return jsonResponse({ username: 'boss', role: 'ADMIN' });
      if (url.includes('/studies?')) return jsonResponse(listResponse([study]));
      if (init?.method === 'DELETE') {
        // `init.headers` is typed as the DOM `HeadersInit`; the harness swaps
        // `globalThis.Headers` for TestHeaders, so the cast goes through
        // `unknown` — the two shapes do not nominally overlap (TS2352).
        deleteHeaders = init.headers as unknown as TestHeaders;
        return jsonResponse(null, 204);
      }
      return jsonResponse({ detail: 'Unexpected test request' }, 404);
    });

    const user = userEvent.setup();
    render(<StudyListPage />);
    expect(await screen.findByText('John Doe')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Delete study' }));
    await user.click(await screen.findByRole('button', { name: 'Confirm Delete' }));

    await waitFor(() => expect(deleteHeaders).toBeDefined());
    expect(deleteHeaders?.get('X-CSRF-Token')).toBe('csrf-value-123');
  });

  it('clears an expired token and redirects to the prefix-aware login URL on 401', async () => {
    window.PAXS_BASE_PATH = '/paxs';
    window.history.replaceState({}, '', '/paxs/studies-ui/');
    localStorage.setItem('paxs_token', 'expired-token');
    const navigation = jest.spyOn(authNavigation, 'assign').mockImplementation(() => {});
    jest.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({ detail: 'Not authenticated' }, 401),
    );

    await expect(fetchStudies(filters)).rejects.toMatchObject({ status: 401 });

    expect(localStorage.getItem('paxs_token')).toBeNull();
    expect(navigation).toHaveBeenCalledWith(
      '/paxs/login?next=%2Fpaxs%2Fstudies-ui%2F',
    );
  });
});
