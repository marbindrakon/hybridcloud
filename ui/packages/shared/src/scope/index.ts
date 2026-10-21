export * from './types';
export * from './parseGroups';
export { resolveIdentity, tenantLabel, PLATFORM_NAMESPACE, SCOPE_CONFIRM_FALLBACK } from './resolveIdentity';
export {
  ScopeProvider,
  useIdentity,
  useTenantScope,
  useOptionalScope,
  useRouterScopeAdapter,
  useConsoleScopeAdapter,
  computeScope,
  landingScope,
  loadIdentity,
  parseScopedPath,
  pageOf,
  scopePrefix,
  preferredTenant,
  recentTenants,
  CONSOLE_ALL_NAMESPACES,
} from './ScopeProvider';
export type { ScopeContextValue, ScopeProviderProps } from './ScopeProvider';
export { TenantScopeSelector } from './TenantScopeSelector';
export type { TenantScopeSelectorProps } from './TenantScopeSelector';
export { NoRoleState, NoAccessState, ChooseTenantState, ScopeLoadingState, ScopeGate } from './NoScopeStates';
