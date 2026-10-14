import * as React from 'react';

/**
 * OCP 4.22 NavHeader resolves perspective icons as:
 *   icon().then((m) => m.default)
 * so the CodeRef target must be a LazyComponent `{ default: Component }`,
 * not the component itself. Match the MCE plugin pattern:
 *   export const icon = { default: MulticlusterIcon }
 */
function PerspectiveIcon(): React.ReactElement {
  return (
    <svg
      width="1em"
      height="1em"
      viewBox="0 0 24 24"
      fill="currentColor"
      aria-hidden
      xmlns="http://www.w3.org/2000/svg"
    >
      <path d="M12 2 2 7l10 5 10-5-10-5z" opacity="0.95" />
      <path d="M4 10.5v5.5l8 4v-5.5l-8-4z" opacity="0.75" />
      <path d="M20 10.5v5.5l-8 4v-5.5l8-4z" opacity="0.55" />
    </svg>
  );
}

export const icon = { default: PerspectiveIcon };

/** Platform admins land on the admin overview, everyone else (tenants) on their own overview. */
export const getLandingPageURL = (
  flags: { [key: string]: boolean },
  _isFirstVisit: boolean,
): string =>
  flags?.HYBRIDSOVEREIGN_PLATFORM_ADMIN ? '/hybridsovereign/overview' : '/hybridsovereign/tenant/overview';

export const getImportRedirectURL = (_namespace: string): string =>
  '/hybridsovereign/overview';
