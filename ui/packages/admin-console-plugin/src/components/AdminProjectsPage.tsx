import '../consoleK8sBootstrap';
import { makeKindListPage } from './AdminEntitiesPage';

export default makeKindListPage('Project', 'Projects', {
  listPath: '/hybridsovereign/projects',
  createKind: 'project',
});
