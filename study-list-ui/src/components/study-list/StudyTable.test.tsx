import { describe, expect, it, jest } from '@jest/globals';
import '@testing-library/jest-dom/jest-globals';
import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { StudyTable } from './StudyTable';
import type { Study, StudyFilters } from '@/types/study';

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

const anonymousStudy: Study = {
  study_uid: '1.2.840.10008.1',
  patient_name: null,
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

describe('StudyTable', () => {
  it.each([
    ['table', 'PAXS Diagnostic', 'diagnostic'], ['grid', 'PAXS Diagnostic', 'diagnostic'],
    ['table', 'Basic Viewer', 'viewer'], ['grid', 'Basic Viewer', 'viewer'],
    ['table', 'Segmentation Viewer', 'segmentation'], ['grid', 'Segmentation Viewer', 'segmentation'],
  ] as const)('launches %s view with %s', async (viewMode, workflow, route) => {
    const user = userEvent.setup();
    const replace = jest.fn();
    const tab = {
      opener: window, closed: false, location: {replace},
      document: {title: '', body: {appendChild: jest.fn()}, createElement: () => ({textContent: ''})},
    } as unknown as Window;
    const open = jest.spyOn(window, 'open').mockReturnValue(tab);
    const inspect = jest.fn();
    render(<StudyTable studies={[anonymousStudy]} loading={false} filters={filters}
      setSort={jest.fn()} viewMode={viewMode} onStudyClick={inspect} onActionClick={jest.fn()} />);

    await user.click(screen.getByRole('button', {name: 'Launch workflow'}));
    expect(screen.getByRole('menuitem', {name: /PAXS Diagnostic/})).toBeInTheDocument();
    expect(screen.getByRole('menuitem', {name: /Basic Viewer/})).toBeInTheDocument();
    expect(screen.getByRole('menuitem', {name: /Segmentation Viewer/})).toBeInTheDocument();
    expect(open).not.toHaveBeenCalled();
    await user.keyboard('{Escape}');
    expect(open).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', {name: 'Launch workflow'}));
    await user.click(screen.getByRole('menuitem', {name: new RegExp(workflow)}));
    expect(replace).toHaveBeenCalledWith(
      `http://localhost/viewer/${route}?StudyInstanceUIDs=1.2.840.10008.1`,
    );
    expect(tab.opener).toBeNull();
    expect(inspect).not.toHaveBeenCalled();
  });
  it('renders a null patient name safely and sends backend-supported sort keys', () => {
    const setSort = jest.fn();
    render(
      <StudyTable
        studies={[anonymousStudy]}
        loading={false}
        filters={filters}
        setSort={setSort}
        viewMode="table"
        onStudyClick={jest.fn()}
        onActionClick={jest.fn()}
      />,
    );

    expect(screen.getByText('Anonymous / Unnamed')).toBeInTheDocument();
    const desktopViewer = screen.getByRole('link', { name: 'Open in desktop viewer' });
    expect(desktopViewer).toHaveAttribute('href', expect.stringContaining('paxsviewer://open?'));
    expect(desktopViewer).not.toHaveAttribute('target');
    fireEvent.click(screen.getByRole('button', { name: 'Patient' }));
    expect(setSort).toHaveBeenLastCalledWith('patient', 'desc');
    fireEvent.click(screen.getByRole('button', { name: 'Images' }));
    expect(setSort).toHaveBeenLastCalledWith('instances', 'desc');
  });

  it('renders the delete control only when the caller is allowed to delete', () => {
    const props = {
      studies: [anonymousStudy],
      loading: false,
      filters,
      setSort: jest.fn(),
      viewMode: 'table' as const,
      onStudyClick: jest.fn(),
      onActionClick: jest.fn(),
    };

    const { unmount } = render(<StudyTable {...props} />);
    expect(screen.queryByRole('button', { name: 'Delete study' })).not.toBeInTheDocument();
    unmount();

    render(<StudyTable {...props} canDelete />);
    expect(screen.getByRole('button', { name: 'Delete study' })).toBeInTheDocument();
  });
});
