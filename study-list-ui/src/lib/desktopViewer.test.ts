import { describe, expect, it } from '@jest/globals';
import { getDesktopViewerUri } from './api';

describe('desktop viewer launch', () => {
  it('includes only the same-origin server address and selected Study UID', () => {
    window.PAXS_PUBLIC_BASE_URL = 'https://paxs.hospital.local/archive';

    const link = new URL(getDesktopViewerUri('1.2.840.1.23'));

    expect(link.protocol).toBe('paxsviewer:');
    expect(link.hostname).toBe('open');
    expect(link.searchParams.get('server')).toBe('https://paxs.hospital.local/archive');
    expect(link.searchParams.get('study')).toBe('1.2.840.1.23');
    expect(link.searchParams.has('token')).toBe(false);
  });
});
