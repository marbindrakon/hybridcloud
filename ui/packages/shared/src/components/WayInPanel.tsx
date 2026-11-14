import React from 'react';
import {
  Alert,
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
import {
  osoAccessGroups,
  osoApiCredentials,
  osoNoAccessGroup,
  osoProjectAndDomain,
  osoReaderGroups,
  osoSignIn,
  osoSignInGroups,
  periodParts,
} from '../forms/openstackAccess';

interface WayInStatus {
  endpoints?: EnvironmentEndpoints;
  site?: { name?: string; console?: string; endpoint?: string; displayName?: string };
  access?: { groups?: unknown[]; signInClient?: string };
  namespaces?: Array<{ name?: string; namespace?: string } | string>;
  projectName?: string;
  domainName?: string;
  region?: string;
  awsAccountId?: string;
  gcpProject?: string;
}

type T = (k: string, o?: Record<string, unknown>) => string;

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

function ConsoleButton({ href, label }: { href: string; label: string }): React.ReactElement {
  return (
    <Button
      component="a"
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      variant="primary"
      icon={<ExternalLinkAltIcon />}
      iconPosition="end"
      className="sc-mb"
    >
      {label}
    </Button>
  );
}

/** "name (ID id)", the name alone, or nothing. */
function NameAndId({ name, id, t }: { name?: string; id?: string; t: T }): React.ReactElement | null {
  if (!name && !id) return null;
  if (!id) return <code>{name}</code>;
  if (!name) return <code>{id}</code>;
  return <span>{t('wayin.nameAndId', { name, id })}</span>;
}

/** "1 day", "8 hours", "90 minutes". */
export function periodText(t: T, minutes: number): string {
  const p = periodParts(minutes);
  return t(`wayin.period.${p.unit}`, { count: p.count });
}

/**
 * The OpenStack environment's card: the dashboard and how to sign in, the access groups that can use it and
 * whether the reader is in one, the facts a tool needs (API endpoint, region, project, domain) and how to get an
 * API credential. Objects written before sign-in existed show what they carry.
 */
function OpenStackWayIn({ item, readerGroups }: { item: K8sResource; readerGroups?: string[] }): React.ReactElement {
  const { t } = useTranslation();
  const st = (item.status ?? {}) as WayInStatus;
  const ep = st.endpoints ?? {};
  const signIn = osoSignIn(item);
  const offered = signIn.state === 'offered';
  const rows = osoAccessGroups(item);
  const signInGroups = osoSignInGroups(item);
  const mine = readerGroups ? osoReaderGroups(item, readerGroups) : [];
  const ids = osoProjectAndDomain(item);
  const api = osoApiCredentials(item);
  const noGroup = osoNoAccessGroup(item);

  const groupsText = rows.length
    ? rows.map((r) => (
        <div key={`${r.name}-${r.access}`}>
          {r.ready
            ? t(r.access === 'View' ? 'wayin.oso.groupView' : 'wayin.oso.groupEdit', { group: r.name })
            : r.message
              ? t('wayin.oso.groupNotReady', { group: r.name, message: r.message })
              : t('wayin.oso.groupNotReadyNoMessage', { group: r.name })}
        </div>
      ))
    : signInGroups.length
      ? signInGroups.join(', ')
      : null;

  const apiText = api
    ? api.form === 'notOffered'
      ? t('wayin.oso.apiCredentialsNotOffered')
      : `${t('wayin.oso.apiCredentialsIntro')} ${
          api.form === 'period'
            ? t('wayin.oso.apiCredentialsPeriod', { period: periodText(t, api.minutes) })
            : t('wayin.oso.apiCredentialsUnknown')
        }`
    : null;

  return (
    <>
      {ep.console ? (
        <ConsoleButton href={ep.console} label={t('wayin.open.CloudOSO')} />
      ) : offered ? (
        <p className="sc-text-muted">{t('wayin.notPublished')}</p>
      ) : null}
      {offered && (
        <p className="sc-mb">
          {signIn.start === 'Dashboard' && signIn.idpName
            ? t('wayin.oso.signInDashboard', { name: signIn.idpName })
            : t('wayin.oso.signInIdentityProvider')}
        </p>
      )}
      {signIn.state === 'notSetUp' && <p className="sc-mb">{t('wayin.oso.siteNoSignIn')}</p>}
      {noGroup && (
        <Alert variant="warning" isInline isPlain title={t('wayin.oso.noGroup')} className="sc-mb" />
      )}
      <DescriptionList isCompact isHorizontal>
        <Row term={t('wayin.oso.groups')}>{groupsText}</Row>
        {offered && signInGroups.length > 0 && readerGroups && (
          <Row term={t('wayin.you')}>
            {mine.length ? t('wayin.youAreIn', { groups: mine.join(', ') }) : t('wayin.oso.youAreNotIn')}
          </Row>
        )}
        <Row term={t('wayin.api')}>{ep.api ? <code>{ep.api}</code> : null}</Row>
        <Row term={t('wayin.region')}>{ep.region}</Row>
        <Row term={t('wayin.project')}>
          <NameAndId name={ids.project} id={ids.projectId} t={t} />
        </Row>
        <Row term={t('wayin.domain')}>
          <NameAndId name={ids.domain} id={ids.domainId} t={t} />
        </Row>
        <Row term={t('wayin.location')}>{ep.location ?? st.site?.displayName ?? st.site?.name}</Row>
        <Row term={t('wayin.oso.apiCredentials')}>{apiText}</Row>
      </DescriptionList>
      {!offered && (
        <p className="sc-text-muted" style={{ marginTop: '0.5rem' }}>
          {t('wayin.openstackScope')}
        </p>
      )}
    </>
  );
}

/** Body of the card for OpenShift Virtualization and public cloud environments. */
function GenericWayIn({
  kind,
  item,
  readerGroups,
}: {
  kind: NetworkPlacementBackendKind;
  item: K8sResource;
  readerGroups?: string[];
}): React.ReactElement {
  const { t } = useTranslation();
  const st = (item.status ?? {}) as WayInStatus;
  const ep = st.endpoints ?? {};
  const consoleUrl = ep.console ?? (kind === 'CloudVirt' ? st.site?.console : undefined);
  const idpName = ep.signIn?.idpName;
  const accessGroups = (st.access?.groups ?? []).filter((g): g is string => typeof g === 'string');
  const groups = ep.signInGroups ?? accessGroups;
  const mine = groups.filter((g) => readerGroups?.includes(g));
  const projects = asList(ep.project).length
    ? asList(ep.project)
    : kind === 'CloudVirt'
      ? (st.namespaces ?? [])
          .map((n) => (typeof n === 'string' ? n : n.namespace ?? ''))
          .filter(Boolean)
      : asList(st.projectName);
  const location = ep.location ?? st.site?.displayName ?? st.site?.name;
  return (
    <>
      {consoleUrl ? (
        <ConsoleButton href={consoleUrl} label={t(`wayin.open.${kind}`)} />
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
        <Row term={t('wayin.domain')}>{ep.domain}</Row>
        <Row term={t('wayin.location')}>{location}</Row>
        <Row term={t('wayin.signInGroups')}>{groups.length ? groups.join(', ') : null}</Row>
        {groups.length > 0 && readerGroups && (
          <Row term={t('wayin.you')}>{mine.length ? t('wayin.youAreIn', { groups: mine.join(', ') }) : t('wayin.youAreNotIn')}</Row>
        )}
      </DescriptionList>
      {kind === 'CloudVirt' && <p className="sc-text-muted" style={{ marginTop: '0.5rem' }}>{t('wayin.virtScope')}</p>}
    </>
  );
}

/**
 * The card "How to use it" with the reader's groups given (no scope needed): what WayInPanel renders.
 * readerGroups undefined: the reader is not known, the row "You" is left out.
 */
export function WayInCard({
  kind,
  item,
  readerGroups,
}: {
  kind: NetworkPlacementBackendKind;
  item: K8sResource;
  readerGroups?: string[];
}): React.ReactElement {
  const { t } = useTranslation();
  return (
    <Card isCompact className="sc-mb sc-wayin">
      <CardTitle>{t('wayin.title')}</CardTitle>
      <CardBody>
        {kind === 'CloudOSO' ? (
          <OpenStackWayIn item={item} readerGroups={readerGroups} />
        ) : (
          <GenericWayIn kind={kind} item={item} readerGroups={readerGroups} />
        )}
      </CardBody>
    </Card>
  );
}

/**
 * "How to use it" for an environment (Q18, UX-067): where to go, which sign-in
 * button to press, where the workloads live and who can sign in. The platform
 * publishes the facts in status.endpoints; before it does, the panel says the
 * console is not published yet instead of showing a dash. The reader's groups
 * are those of the platform sign-in.
 */
export function WayInPanel({ kind, item }: { kind: NetworkPlacementBackendKind; item: K8sResource }): React.ReactElement {
  const identity = useOptionalScope()?.identity;
  return <WayInCard kind={kind} item={item} readerGroups={identity?.groups} />;
}
