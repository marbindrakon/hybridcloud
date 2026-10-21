import React from 'react';
import {
  Button,
  Card,
  CardBody,
  CardTitle,
  DescriptionList,
  DescriptionListDescription,
  DescriptionListGroup,
  DescriptionListTerm,
} from '@patternfly/react-core';
import { ExternalLinkAltIcon } from '@patternfly/react-icons';
import { useTranslation } from '../i18n';
import { useOptionalScope } from '../scope/ScopeProvider';
import type { EnvironmentEndpoints, K8sResource, NetworkPlacementBackendKind } from '../types';

interface WayInStatus {
  endpoints?: EnvironmentEndpoints;
  site?: { name?: string; console?: string; endpoint?: string; displayName?: string };
  access?: { groups?: string[]; signInClient?: string };
  namespaces?: Array<{ name?: string; namespace?: string } | string>;
  projectName?: string;
  domainName?: string;
  region?: string;
  awsAccountId?: string;
  gcpProject?: string;
}

function asList(v: string | string[] | undefined): string[] {
  if (!v) return [];
  return Array.isArray(v) ? v : [v];
}

function Row({ term, children }: { term: string; children: React.ReactNode }): React.ReactElement | null {
  if (children === undefined || children === null || children === '') return null;
  return (
    <DescriptionListGroup>
      <DescriptionListTerm>{term}</DescriptionListTerm>
      <DescriptionListDescription>{children}</DescriptionListDescription>
    </DescriptionListGroup>
  );
}

/**
 * "How to use it" for an environment (Q18, UX-067): where to go, which sign-in
 * button to press, where the workloads live and who can sign in. The platform
 * publishes the facts in status.endpoints; before it does, the panel says the
 * console is not published yet instead of showing a dash.
 */
export function WayInPanel({ kind, item }: { kind: NetworkPlacementBackendKind; item: K8sResource }): React.ReactElement {
  const { t } = useTranslation();
  const identity = useOptionalScope()?.identity;
  const st = (item.status ?? {}) as WayInStatus;
  const ep = st.endpoints ?? {};
  const consoleUrl = ep.console ?? (kind === 'CloudVirt' ? st.site?.console : undefined);
  const idpName = ep.signIn?.idpName;
  const groups = ep.signInGroups ?? st.access?.groups ?? [];
  const mine = groups.filter((g) => identity?.groups.includes(g));
  const projects = asList(ep.project).length
    ? asList(ep.project)
    : kind === 'CloudVirt'
      ? (st.namespaces ?? [])
          .map((n) => (typeof n === 'string' ? n : n.namespace ?? ''))
          .filter(Boolean)
      : asList(st.projectName);
  const location = ep.location ?? st.site?.displayName ?? st.site?.name;

  return (
    <Card isCompact className="sc-mb sc-wayin">
      <CardTitle>{t('wayin.title')}</CardTitle>
      <CardBody>
        {consoleUrl ? (
          <Button
            component="a"
            href={consoleUrl}
            target="_blank"
            rel="noopener noreferrer"
            variant="primary"
            icon={<ExternalLinkAltIcon />}
            iconPosition="end"
            className="sc-mb"
          >
            {t(`wayin.open.${kind}`)}
          </Button>
        ) : (
          <p className="sc-text-muted">{kind === 'CloudAWS' || kind === 'CloudGCP' ? t('wayin.noConsoleSignIn') : t('wayin.notPublished')}</p>
        )}
        <DescriptionList isCompact isHorizontal>
          <Row term={t('wayin.signInWith')}>
            {idpName ? t('wayin.signInButton', { name: idpName }) : ep.signIn?.method === 'NotAvailable' ? t('wayin.signInNotOffered') : null}
          </Row>
          <Row term={t('wayin.api')}>{ep.api ? <code>{ep.api}</code> : null}</Row>
          <Row term={kind === 'CloudVirt' ? t('wayin.namespaces') : t('wayin.project')}>
            {projects.length ? projects.map((p) => <code key={p} style={{ marginRight: '0.5rem' }}>{p}</code>) : null}
          </Row>
          <Row term={t('wayin.domain')}>{ep.domain ?? (kind === 'CloudOSO' ? st.domainName : undefined)}</Row>
          <Row term={t('wayin.location')}>{location}</Row>
          <Row term={t('wayin.signInGroups')}>{groups.length ? groups.join(', ') : null}</Row>
          {groups.length > 0 && identity && (
            <Row term={t('wayin.you')}>{mine.length ? t('wayin.youAreIn', { groups: mine.join(', ') }) : t('wayin.youAreNotIn')}</Row>
          )}
        </DescriptionList>
        {kind === 'CloudVirt' && <p className="sc-text-muted" style={{ marginTop: '0.5rem' }}>{t('wayin.virtScope')}</p>}
        {kind === 'CloudOSO' && ep.signIn?.method !== 'TenantSSO' && (
          <p className="sc-text-muted" style={{ marginTop: '0.5rem' }}>{t('wayin.openstackScope')}</p>
        )}
      </CardBody>
    </Card>
  );
}
