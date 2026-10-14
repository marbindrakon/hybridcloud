import '../consoleK8sBootstrap';
import { makeKindListPage } from './AdminEntitiesPage';

export default makeKindListPage('Persona', 'Personas', {
  createKind: 'persona',
  listPath: '/hybridsovereign/personas',
});
