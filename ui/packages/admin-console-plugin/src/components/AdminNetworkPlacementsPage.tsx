import '../consoleK8sBootstrap';
import { makeKindListPage } from './AdminEntitiesPage';

/** Network placements of every entity (read-only). */
export default makeKindListPage('NetworkPlacement', 'Network Placements', {
  listPath: '/hybridsovereign/networking/placements',
  readOnly: true,
  subtitleKey: 'pages.allEntitiesReadOnly',
});
