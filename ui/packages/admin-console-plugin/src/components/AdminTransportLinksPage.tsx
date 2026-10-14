import '../consoleK8sBootstrap';
import { makeKindListPage } from './AdminEntitiesPage';

export default makeKindListPage('TransportLink', 'Transport Links', {
  listPath: '/hybridsovereign/networking/transport',
  createKind: 'transportlink',
});
