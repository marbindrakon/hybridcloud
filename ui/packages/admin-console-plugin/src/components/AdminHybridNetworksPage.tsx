import '../consoleK8sBootstrap';
import { makeKindListPage } from './AdminEntitiesPage';

/** Tenant networks of every entity (read-only). */
export default makeKindListPage('HybridNetwork', 'Hybrid Networks', {
  listPath: '/hybridsovereign/networking/networks',
  readOnly: true,
  subtitleKey: 'pages.allEntitiesReadOnly',
});
