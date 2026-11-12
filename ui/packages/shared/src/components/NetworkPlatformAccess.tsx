import React from 'react';
import { DescriptionList, DescriptionListDescription, DescriptionListGroup, DescriptionListTerm, ExpandableSection, Label } from '@patternfly/react-core';
import { Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { useTranslation } from '../i18n';
import { useTenantProfile } from '../hooks/tenantProfile';
import { findCondition } from '../status/phase';
import { PLATFORM_ACCESS_OFF_REASONS, platformReachBlock, reasonText } from '../forms/clusterNetworkChoice';
import type { HybridNetwork, TenantProfile } from '../types';
import { AllowPlatformAccessButton } from './AllowPlatformAccessButton';

function Row({ label, children }: { label: string; children: React.ReactNode }): React.ReactElement {
  return (
    <DescriptionListGroup>
      <DescriptionListTerm>{label}</DescriptionListTerm>
      <DescriptionListDescription>{children ?? '—'}</DescriptionListDescription>
    </DescriptionListGroup>
  );
}

function Mono({ items }: { items?: Array<string | number> | null }): React.ReactElement {
  if (!items || !items.length) return <>—</>;
  return <>{items.map((i, n) => <React.Fragment key={String(i)}>{n > 0 ? ', ' : ''}<code>{i}</code></React.Fragment>)}</>;
}

/**
 * Platform access of a network: whether the tenant allows it (spec.platformAccess, on by default),
 * whether the platform reaches the network (condition PlatformReachable) and why not, the platform's
 * own addresses and ports (for the tenant's firewalls) and the cluster addresses it reaches.
 */
export function PlatformAccessSection({
  network,
  profile,
  onChanged,
  readOnly,
}: {
  network: HybridNetwork;
  profile?: TenantProfile | null;
  onChanged?: () => void;
  readOnly?: boolean;
}): React.ReactElement {
  const { t } = useTranslation();
  const allowed = network.spec?.platformAccess !== false;
  const st = network.status?.platformAccess;
  const cond = findCondition(network, 'PlatformReachable');
  const block = cond
    ? platformReachBlock(network, {
        managedRanges: profile?.status?.managedClusterRanges,
        reserved: profile?.status?.reservedRanges,
      })
    : null;
  const prefixes = Array.isArray(st?.prefixes) ? st?.prefixes ?? [] : [];
  // where the platform offers no connection for clusters on networks there are no platform addresses to admit
  const offered = !(cond && cond.status !== 'True' && cond.reason === 'ServiceNotOffered');
  return (
    <section style={{ marginBottom: '1.25rem' }} aria-label={t('platformAccess.title')}>
      <h3 className="sc-netov__title" style={{ marginBottom: '0.5rem' }}>{t('platformAccess.title')}</h3>
      <p className="sc-text-muted">{t('platformAccess.help')}</p>
      <DescriptionList isCompact isHorizontal className="sc-mb">
        <Row label={t('platformAccess.switch')}>{allowed ? t('common.enabled') : t('common.disabled')}</Row>
        <Row label={t('platformAccess.reachable')}>
          {!cond ? (
            t('platformAccess.unknown')
          ) : cond.status === 'True' ? (
            <Label color="green" isCompact>
              {t('platformAccess.yes')}
            </Label>
          ) : (
            <>
              <Label color="grey" isCompact>
                {t('platformAccess.no')}
              </Label>{' '}
              {block ? reasonText(t, block) : cond.message}{' '}
              {!readOnly && block && PLATFORM_ACCESS_OFF_REASONS.includes(block.reason) && (
                <AllowPlatformAccessButton network={network} variant="link" isInline onDone={onChanged} />
              )}
            </>
          )}
        </Row>
        {st?.state && (
          <Row label={t('platformAccess.state')}>{t(`platformAccess.state_${st.state}`, { defaultValue: st.state })}</Row>
        )}
        {/* the reason in words is shown above; the API's own message only when there is none */}
        {st?.message && !block && <Row label={t('platformAccess.why')}>{st.message}</Row>}
        {offered && (
          <Row label={t('platformAccess.sources')}>
            <Mono items={st?.sources} />
          </Row>
        )}
        {offered && (
          <Row label={t('platformAccess.ports')}>
            <Mono items={st?.ports} />
          </Row>
        )}
      </DescriptionList>
      {prefixes.length === 0 ? (
        <p className="sc-text-muted">{t('platformAccess.noPrefixes')}</p>
      ) : (
        <Table aria-label={t('platformAccess.prefixes')} variant="compact" className="sc-mb">
          <Thead>
            <Tr>
              <Th modifier="wrap">{t('platformAccess.prefix')}</Th>
              <Th modifier="wrap">{t('platformAccess.cluster')}</Th>
              <Th modifier="wrap">{t('platformAccess.state')}</Th>
              <Th modifier="wrap">{t('platformAccess.why')}</Th>
            </Tr>
          </Thead>
          <Tbody>
            {prefixes.map((p) => (
              <Tr key={p.prefix}>
                <Td dataLabel={t('platformAccess.prefix')}>
                  <code>{p.prefix}</code>
                </Td>
                <Td dataLabel={t('platformAccess.cluster')}>{p.cluster || '—'}</Td>
                <Td dataLabel={t('platformAccess.state')}>
                  {p.state ? t(`platformAccess.prefixState_${p.state}`, { defaultValue: p.state }) : '—'}
                </Td>
                <Td dataLabel={t('platformAccess.why')}>{p.reason || p.message ? reasonText(t, { reason: p.reason, message: p.message }) : '—'}</Td>
              </Tr>
            ))}
          </Tbody>
        </Table>
      )}
    </section>
  );
}

/**
 * "Address ranges reserved by the platform" from the tenant profile: each range, its purpose,
 * whether networks are refused in it or the platform only does not allocate from it, and the site
 * a row applies to; then the ranges for clusters managed by the platform.
 */
export function ReservedRangesSection({
  profile,
  collapsible,
}: {
  profile: TenantProfile | null | undefined;
  /** In a form: an expandable section, closed */
  collapsible?: boolean;
}): React.ReactElement {
  const { t } = useTranslation();
  const ranges = profile?.status?.reservedRanges;
  const managed = profile?.status?.managedClusterRanges;
  const body = (
    <>
      <p className="sc-text-muted">{t('reserved.intro')}</p>
      {!Array.isArray(ranges) ? (
        <p className="sc-text-muted">{t('reserved.notListed')}</p>
      ) : ranges.length === 0 ? (
        <p className="sc-text-muted">{t('reserved.none')}</p>
      ) : (
        <Table aria-label={t('reserved.title')} variant="compact" className="sc-mb">
          <Thead>
            <Tr>
              <Th modifier="wrap">{t('reserved.range')}</Th>
              <Th modifier="wrap">{t('reserved.purpose')}</Th>
              <Th modifier="wrap">{t('reserved.rule')}</Th>
              <Th modifier="wrap">{t('reserved.site')}</Th>
            </Tr>
          </Thead>
          <Tbody>
            {ranges.map((r) => (
              <Tr key={`${r.prefix} ${r.site ?? ''}`}>
                <Td dataLabel={t('reserved.range')}>
                  <code>{r.prefix}</code>
                </Td>
                <Td dataLabel={t('reserved.purpose')}>{r.purpose || '—'}</Td>
                <Td dataLabel={t('reserved.rule')}>{r.rule ? t(`reserved.rule_${r.rule}`, { defaultValue: r.rule }) : '—'}</Td>
                <Td dataLabel={t('reserved.site')}>{r.site || t('reserved.allSites')}</Td>
              </Tr>
            ))}
          </Tbody>
        </Table>
      )}
      <h4 className="sc-netov__title" style={{ marginBottom: '0.5rem' }}>{t('reserved.managedTitle')}</h4>
      <p className="sc-text-muted">{t('reserved.managedHelp')}</p>
      <p>
        {Array.isArray(managed) && managed.includes('0.0.0.0/0') ? (
          t('reserved.managedAny')
        ) : Array.isArray(managed) && managed.length ? (
          <Mono items={managed} />
        ) : Array.isArray(ranges) ? (
          t('reserved.managedNotOffered')
        ) : (
          t('reserved.managedNotListed')
        )}
      </p>
    </>
  );
  if (collapsible) {
    return (
      <ExpandableSection toggleText={t('reserved.title')} className="sc-mb">
        {body}
      </ExpandableSection>
    );
  }
  return (
    <section style={{ marginBottom: '1.25rem' }} aria-label={t('reserved.title')}>
      <h3 className="sc-netov__title" style={{ marginBottom: '0.5rem' }}>{t('reserved.title')}</h3>
      {body}
    </section>
  );
}

/** The two sections of the network page, with the tenant profile read once. */
export function NetworkPlatformSections({ network, readOnly }: { network: HybridNetwork; readOnly?: boolean }): React.ReactElement {
  const { profile } = useTenantProfile(network.metadata?.namespace);
  return (
    <>
      <PlatformAccessSection network={network} profile={profile} readOnly={readOnly} />
      <ReservedRangesSection profile={profile} />
    </>
  );
}
