/**
 * PaxsViewer (PaxsViewer v3 fork) — PAXS Archive data source configuration.
 *
 * Served from app/static/viewer/app-config.js (same-origin as the API), so the
 * viewer's DICOMweb requests to /dicom-web need no CORS and no token: the PAXS
 * DICOMweb endpoints accept the archive's guest/anonymous scope, and tenant
 * scoping follows the request's session exactly like the dashboard.
 *
 * Deep links use the standard PaxsViewer query form: /viewer?StudyInstanceUIDs=1.2.3.4
 *
 * Deployment: copy the built dist from the fork
 * (D:\Github\Viewers\platform\app\dist, built with PUBLIC_URL=/viewer/) into
 * app/static/viewer/ and drop this file next to it (see PAXSVIEWER-INTEGRATION.md).
 */
window.config = {
  routerBasename: '/viewer',
  extensions: [],
  modes: [],
  showStudyList: true,
  maxNumberOfWebWorkers: 3,
  showWarningMessageForCrossOrigin: true,
  showCPUFallbackMessage: true,
  showLoadingIndicator: true,
  experimentalStudyBrowserSort: false,
  strictZSpacingForVolumeViewport: true,
  groupEnabledModesFirst: true,
  defaultDataSourceName: 'paxs',
  dataSources: [
    {
      namespace: '@ohif/extension-default.dataSourcesModule.dicomweb',
      sourceName: 'paxs',
      configuration: {
        friendlyName: 'PAXS Archive',
        name: 'PAXS',
        wadoUriRoot: '/dicom-web',
        qidoRoot: '/dicom-web',
        wadoRoot: '/dicom-web',
        qidoSupportsIncludeField: true,
        supportsReject: false,
        supportsStow: true,
        imageRendering: 'wadors',
        thumbnailRendering: 'wadors',
        enableStudyLazyLoad: true,
        supportsFuzzyMatching: true,
        supportsWildcard: true,
        singlepart: 'bulkdata,video,pdf',
        omitQuotationForMultipartRequest: true,
      },
    },
  ],
  httpErrorHandler: (error) => {
    if (error && error.status === 429) {
      console.warn('[PAXS] rate-limited by the archive — slow down the queries.');
    } else if (error && error.status === 401) {
      console.warn('[PAXS] DICOMweb request rejected (401) — sign in on the dashboard first.');
    }
  },
};
