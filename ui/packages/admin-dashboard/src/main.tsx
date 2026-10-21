import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import '@patternfly/react-core/dist/styles/base.css';
import '@hybridsovereign/shared/styles/openshift.css';
import { configureK8sClient, configurePermissionsClient } from '@hybridsovereign/shared';
import { App } from './App';

// Admin uses the raw API proxy (/api/k8s) with the caller's token
configureK8sClient({ apiStyle: 'raw', baseUrl: '/api/k8s' });
configurePermissionsClient('/api/k8s', { style: 'can-i' });

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>,
);
