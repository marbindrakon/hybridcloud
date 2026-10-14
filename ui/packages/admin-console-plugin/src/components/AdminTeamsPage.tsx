import '../consoleK8sBootstrap';
import { makeKindListPage } from './AdminEntitiesPage';

export default makeKindListPage('Team', 'Teams', {
  listPath: '/hybridsovereign/teams',
  createKind: 'team',
});
