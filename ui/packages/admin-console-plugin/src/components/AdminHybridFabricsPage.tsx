import '../consoleK8sBootstrap';
import { makeKindListPage } from './AdminEntitiesPage';

export default makeKindListPage('HybridFabric', 'Hybrid Fabrics', {
  listPath: '/hybridsovereign/networking/fabrics',
  createKind: 'hybridfabric',
});
