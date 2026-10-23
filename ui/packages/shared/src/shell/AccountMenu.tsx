import React, { useState } from 'react';
import { Divider, Dropdown, DropdownGroup, DropdownItem, DropdownList, MenuToggle } from '@patternfly/react-core';
import { UserIcon } from '@patternfly/react-icons';
import { useTranslation } from '../i18n';
import { useOptionalScope } from '../scope/ScopeProvider';
import { tenantLabel } from '../scope/resolveIdentity';

const GROUPS_SHOWN = 5;

/** Ends the session: the server revokes this token, then the proxy clears its cookie. */
export async function signOut(): Promise<void> {
  let redirect = '/oauth/sign_out';
  try {
    const r = await fetch('/api/session/end', { method: 'POST', headers: { Accept: 'application/json' } });
    if (r.ok) {
      const body = (await r.json()) as { redirect?: string };
      if (body.redirect && body.redirect.startsWith('/')) redirect = body.redirect;
    }
  } catch {
    /* the proxy sign-out still clears the session cookie */
  }
  window.location.assign(redirect);
}

/** Who am I: name, tenant, groups, sign out (UX-003, UX-004). */
export function AccountMenu(): React.ReactElement | null {
  const { t } = useTranslation();
  const ctx = useOptionalScope();
  const [open, setOpen] = useState(false);
  const [allGroups, setAllGroups] = useState(false);
  const identity = ctx?.identity;
  if (!identity) return null;
  const name = identity.displayName || identity.username;
  const scope = ctx?.scope;
  const groups = identity.groups.filter((g) => !g.startsWith('system:'));
  const shown = allGroups ? groups : groups.slice(0, GROUPS_SHOWN);
  return (
    <Dropdown
      isOpen={open}
      onOpenChange={setOpen}
      popperProps={{ position: 'right' }}
      toggle={(ref) => (
        <MenuToggle ref={ref} variant="plainText" icon={<UserIcon />} onClick={() => setOpen((o) => !o)} isExpanded={open} aria-label={t('shell.account.menu')}>
          {name}
        </MenuToggle>
      )}
    >
      <DropdownGroup label={t('shell.account.signedInAs')}>
        <DropdownList>
          <DropdownItem key="user" isDisabled description={identity.displayName ? identity.username : undefined}>
            {name}
          </DropdownItem>
          {scope?.kind === 'tenant' && (
            <DropdownItem key="tenant" isDisabled description={scope.tenant.namespace}>
              {t('scope.tenant')}: {tenantLabel(scope.tenant)}
            </DropdownItem>
          )}
          {scope?.kind === 'all' && (
            <DropdownItem key="tenant" isDisabled>
              {t('scope.allTenants')}
            </DropdownItem>
          )}
          {identity.platform.admin && (
            <DropdownItem key="platform" isDisabled>
              {t('shell.account.platformAdmin')}
            </DropdownItem>
          )}
        </DropdownList>
      </DropdownGroup>
      <Divider />
      <DropdownGroup label={t('shell.account.groups', { count: groups.length })}>
        <DropdownList>
          {shown.length === 0 && (
            <DropdownItem key="nogroups" isDisabled>
              {t('shell.account.noGroups')}
            </DropdownItem>
          )}
          {shown.map((g) => (
            <DropdownItem key={`g-${g}`} isDisabled>
              {g}
            </DropdownItem>
          ))}
          {groups.length > GROUPS_SHOWN && !allGroups && (
            <DropdownItem
              key="more"
              onClick={(e) => {
                e?.stopPropagation?.();
                setAllGroups(true);
              }}
            >
              {t('shell.account.showAllGroups', { count: groups.length })}
            </DropdownItem>
          )}
        </DropdownList>
      </DropdownGroup>
      <Divider />
      <DropdownList>
        <DropdownItem key="signout" onClick={() => void signOut()}>
          {t('shell.account.signOut')}
        </DropdownItem>
      </DropdownList>
    </Dropdown>
  );
}
