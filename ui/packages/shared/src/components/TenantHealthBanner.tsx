import React from 'react';
import { Alert } from '@patternfly/react-core';
import { useTranslation } from '../i18n';
import { useTenantProfile } from '../hooks/tenantProfile';
import { reasonEntry } from '../status/phase';
import type { TenantProfileCondition } from '../types';

/**
 * The tenant's own controller health, from its profile's conditions: a
 * warning while changes in the tenant are delayed (ControllerAvailable False
 * or DeliveryStalled True). Shows the registry sentence of the reason, never
 * the raw condition message; nothing when the profile cannot be read.
 */
export function tenantDelayCondition(conditions: TenantProfileCondition[] | undefined): TenantProfileCondition | undefined {
  const list = conditions ?? [];
  return (
    list.find((c) => c.type === 'ControllerAvailable' && c.status === 'False') ??
    list.find((c) => c.type === 'DeliveryStalled' && c.status === 'True')
  );
}

export function TenantHealthBanner({ namespace }: { namespace: string }): React.ReactElement | null {
  const { t } = useTranslation();
  const { profile } = useTenantProfile(namespace, { enabled: !!namespace });
  const cond = tenantDelayCondition(profile?.status?.conditions);
  if (!cond) return null;
  const summary = reasonEntry(cond.reason)?.summary ?? t('callout.tenantDelayed');
  return (
    <Alert variant="warning" isInline title={t('callout.tenantDelayedTitle')} className="sc-mb">
      {summary}
    </Alert>
  );
}
