import '../consoleK8sBootstrap';
import { makeKindListPage } from './AdminEntitiesPage';

export default makeKindListPage('CloudGateway', 'Cloud Gateways', {
  listPath: '/hybridsovereign/networking/gateways',
  createKind: 'cloudgateway',
});
