import {afterEach, describe, expect, it} from '@jest/globals';
import {getViewerUrl} from './api';

afterEach(() => {
  delete window.PAXS_PUBLIC_BASE_URL;
  delete window.PAXS_BASE_PATH;
});

describe('viewer workflow links', () => {
  it('preserves Basic Viewer as the backward-compatible default', () => {
    expect(getViewerUrl('1.2.3')).toBe('http://localhost/viewer/viewer?StudyInstanceUIDs=1.2.3');
  });

  it('opens the PAXS diagnostic mode with the study the launch was given', () => {
    // The exact link the study list is expected to produce for a selected study.
    expect(getViewerUrl('1.3.12.2.1107.5.2.67.223876.30000026062407093745500000007', 'diagnostic')).toBe(
      'http://localhost/viewer/diagnostic?StudyInstanceUIDs=1.3.12.2.1107.5.2.67.223876.30000026062407093745500000007',
    );
  });

  it('retains the reverse-proxy base for the diagnostic mode too', () => {
    window.PAXS_PUBLIC_BASE_URL = 'https://paxs.example.test/archive';
    expect(getViewerUrl('1.2.3', 'diagnostic')).toBe(
      'https://paxs.example.test/archive/viewer/diagnostic?StudyInstanceUIDs=1.2.3',
    );
  });

  it('opens the registered segmentation mode and retains the reverse-proxy base', () => {
    window.PAXS_PUBLIC_BASE_URL = 'https://paxs.example.test/archive';
    expect(getViewerUrl('1.2.3', 'segmentation')).toBe(
      'https://paxs.example.test/archive/viewer/segmentation?StudyInstanceUIDs=1.2.3',
    );
  });

  it('encodes the selected study without accepting extra query parameters', () => {
    const url = new URL(getViewerUrl('1.2.3&other=4', 'segmentation'));
    expect(url.searchParams.get('StudyInstanceUIDs')).toBe('1.2.3&other=4');
    expect(url.searchParams.has('other')).toBe(false);
    expect(url.pathname).toBe('/viewer/segmentation');
  });
});
