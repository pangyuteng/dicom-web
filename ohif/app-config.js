// OHIF Viewer configuration for connecting to our local DICOMweb server
// Mount this file into the ohif/app container at /usr/share/nginx/html/app-config.js

window.config = {
  routerBasename: '/',
  extensions: [],
  modes: [],
  showStudyList: true,

  // This is the key section for DICOMweb
  dataSources: [
    {
      namespace: '@ohif/extension-default.dataSourcesModule.dicomweb',
      sourceName: 'dicomweb',
      configuration: {
        friendlyName: 'Local DICOMweb Server',
        name: 'dicomweb',

        // These must be reachable from the *browser*, not just inside Docker.
        // Since we expose dicomweb on host port 5985, use localhost from the browser.
        wadoUriRoot: 'http://192.168.68.143:5985',
        qidoRoot: 'http://192.168.68.143:5985',
        wadoRoot: 'http://192.168.68.143:5985',

        qidoSupportsIncludeField: true,
        supportsReject: false,
        supportsStow: true,                // Enable upload via OHIF (uses our STOW-RS)
        imageRendering: 'wadouri',         // Works reliably with our server (alternative: 'wadors')
        thumbnailRendering: 'wadors',

        enableStudyLazyLoad: true,
        supportsFuzzyMatching: true,
        supportsWildcard: true,
      },
    },
  ],

  defaultDataSourceName: 'dicomweb',

  // Optional: behavior tweaks
  enableGoogleCloudAdapter: false,
  showPatientInfo: true,
  maxNumberOfWebWorkers: 3,
};
