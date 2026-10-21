export * from './permissionGate';
export * from './errorText';
export { hintRoles } from './roleHints';
export { ROLE_HINTS, TENANT_ROLE_KEYS } from './roleHints.generated';
export type { TenantRoleKey } from './roleHints.generated';
export { selfSubjectAccessReview } from './ssar';
export type { ResourceAttributes } from './ssar';
export {
  PermissionProvider,
  usePermission,
  usePermissionsFor,
  useScopeGateNamespace,
  useGateMatrix,
} from './PermissionProvider';
export type { UsePermissionResult, PermissionReason } from './PermissionProvider';
export { GatedButton } from './GatedButton';
export type { GatedButtonProps } from './GatedButton';
export { ForbiddenState, UnavailableState, UnavailablePage } from './ForbiddenState';
