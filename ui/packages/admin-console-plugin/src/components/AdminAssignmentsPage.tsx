import '../consoleK8sBootstrap';
import { makeKindListPage } from './AdminEntitiesPage';

export default makeKindListPage('Assignment', 'Assignments', {
  listPath: '/hybridsovereign/assignments',
  createKind: 'assignment',
});
