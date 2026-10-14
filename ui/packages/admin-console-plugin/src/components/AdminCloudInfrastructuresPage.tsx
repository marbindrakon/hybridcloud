import '../consoleK8sBootstrap';
import { makeKindListPage } from './AdminEntitiesPage';

export default makeKindListPage('CloudInfrastructure', 'Cloud Infrastructure', {
  listPath: '/hybridsovereign/clouds/infrastructure',
  createKind: 'cloudinfrastructure',
  namespace: 'sovereign-cloud',
});
