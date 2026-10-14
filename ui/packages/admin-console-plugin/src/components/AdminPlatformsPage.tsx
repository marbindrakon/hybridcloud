import '../consoleK8sBootstrap';
import { makeKindListPage } from './AdminEntitiesPage';

export default makeKindListPage('PlatformOpenshift', 'Platforms', {
  listPath: '/hybridsovereign/platforms',
  createKind: 'platformopenshift',
});
