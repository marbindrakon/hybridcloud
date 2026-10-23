import React from 'react';
import { Button, Tooltip, type ButtonProps } from '@patternfly/react-core';
import { useTranslation } from '../i18n';
import type { HybridSovereignKind } from '../types';
import { usePermission } from './PermissionProvider';
import { roleHintText } from './errorText';
import type { GateVerb } from './permissionGate';

export interface GatedButtonProps extends Omit<ButtonProps, 'ref'> {
  kind: HybridSovereignKind;
  verb: GateVerb;
  /** Evaluate in this namespace instead of the current scope's. */
  namespace?: string;
  /** Extra condition (for example read-only All tenants scope); false disables with `disabledReason`. */
  enabled?: boolean;
  disabledReason?: string;
}

/**
 * A button that is disabled up front when the caller may not do the action,
 * with a tooltip naming the roles that grant it and the tenant contacts
 * (design section 6, "403 on an action"). The API stays authoritative.
 */
export function GatedButton({
  kind,
  verb,
  namespace,
  enabled = true,
  disabledReason,
  children,
  ...rest
}: GatedButtonProps): React.ReactElement {
  const { t } = useTranslation();
  const { allowed, loading, reason } = usePermission(kind, verb, { namespace });
  const ok = allowed && enabled;
  const button = (
    <Button {...rest} isAriaDisabled={!ok || rest.isAriaDisabled} isDisabled={undefined}>
      {children}
    </Button>
  );
  if (ok) return button;
  const tip = loading
    ? t('access.checking')
    : !enabled && disabledReason
      ? disabledReason
      : [
          t('access.notAllowed'),
          roleHintText(t, {
            roleNames: (reason?.roles ?? []).map((r) => t(`roles.${r}`)),
            contacts: reason?.contacts,
          }),
        ]
          .filter(Boolean)
          .join(' ');
  return <Tooltip content={tip}>{button}</Tooltip>;
}
