import React from 'react';
import { Button, Card, CardBody, CardTitle, Label } from '@patternfly/react-core';
import { CheckCircleIcon, ExclamationTriangleIcon, ExternalLinkAltIcon, InfoCircleIcon } from '@patternfly/react-icons';
import { useTranslation } from '../i18n';
import { condStatus, findCondition, reasonEntry } from '../status/phase';
import type { HybridSovereignKind, K8sResource } from '../types';

/** Kinds whose status names where people sign in (loginUrl) and their organization page (orgUrl). */
export const TOOL_SIGN_IN_KINDS: ReadonlySet<HybridSovereignKind> = new Set<HybridSovereignKind>(['AAPOrg', 'QuayOrg', 'VaultKV', 'Vault']);

function webUrl(v: unknown): string | undefined {
  return typeof v === 'string' && /^https?:\/\//.test(v) ? v : undefined;
}

/**
 * "Sign in" for a platform service object (AAPOrg, QuayOrg, VaultKV, Vault): the
 * sign-in page (status.loginUrl), the organization's page (status.orgUrl) and
 * whether sign-in was verified for the granted access groups (SignInVerified).
 */
export function ToolSignInPanel({ kind, item }: { kind: HybridSovereignKind; item: K8sResource }): React.ReactElement | null {
  const { t } = useTranslation();
  if (!TOOL_SIGN_IN_KINDS.has(kind)) return null;
  const st = (item.status ?? {}) as Record<string, unknown>;
  const loginUrl = webUrl(st.loginUrl);
  const orgUrl = webUrl(st.orgUrl);
  const verified = findCondition(item, 'SignInVerified');
  if (!loginUrl && !orgUrl && !verified) return null;
  const tool = t(`toolSignIn.tool.${kind}`);
  const reason = verified?.reason;
  const state = verified ? condStatus(verified.status) : undefined;
  const summary = reason ? reasonEntry(reason)?.summary : undefined;
  let chip: React.ReactNode = null;
  if (state === 'True') {
    chip = (
      <Label color="green" icon={<CheckCircleIcon />}>
        {t('toolSignIn.verified')}
      </Label>
    );
  } else if (state === 'False' && reason === 'NoMembersYet') {
    chip = (
      <Label color="blue" icon={<InfoCircleIcon />}>
        {t('toolSignIn.noMembersYet')}
      </Label>
    );
  } else if (state === 'False') {
    chip = (
      <Label color="orange" icon={<ExclamationTriangleIcon />}>
        {t('toolSignIn.notVerified')}
      </Label>
    );
  } else if (verified) {
    chip = <Label color="grey">{t('toolSignIn.checking')}</Label>;
  }
  return (
    <Card isCompact className="sc-mb sc-wayin">
      <CardTitle>{t('toolSignIn.title')}</CardTitle>
      <CardBody>
        <div className="sc-chip-row" style={{ display: 'flex', flexWrap: 'wrap', gap: '0.5rem', alignItems: 'center' }}>
          {loginUrl && (
            <Button component="a" href={loginUrl} target="_blank" rel="noopener noreferrer" variant="primary" icon={<ExternalLinkAltIcon />} iconPosition="end">
              {t('toolSignIn.signIn', { tool })}
            </Button>
          )}
          {orgUrl && (
            <Button component="a" href={orgUrl} target="_blank" rel="noopener noreferrer" variant="secondary" icon={<ExternalLinkAltIcon />} iconPosition="end">
              {t('toolSignIn.openIn', { tool })}
            </Button>
          )}
          {chip}
        </div>
        {/* The registry sentence only (tenant-safe); the raw condition message stays on the Conditions tab. */}
        {state === 'False' && summary && (
          <p className="sc-text-muted" style={{ marginTop: '0.5rem' }}>
            {summary}
          </p>
        )}
      </CardBody>
    </Card>
  );
}
