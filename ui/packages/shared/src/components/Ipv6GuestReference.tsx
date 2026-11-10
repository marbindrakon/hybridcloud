/**
 * What a guest on a placement with an IPv6 subnet needs, from the placement's status, and the facts that hold for the
 * placement's environment type. No configuration to paste: the platform does not build the VMs, and a configuration
 * that suits one image misleads on another. Shown only where the placement's site reports IPv6: the same capability
 * check that gates the placement form's IPv6 inputs.
 */
import React from 'react';
import {
  ClipboardCopy,
  DescriptionList,
  DescriptionListDescription,
  DescriptionListGroup,
  DescriptionListTerm,
  ExpandableSection,
} from '@patternfly/react-core';
import { useTranslation } from '../i18n';
import type { NetworkPlacement } from '../types';
import { GUEST_REFERENCE_DOC, guestReference, showsGuestReference, type SiteIpv6Caps } from '../forms/ipv6';
import { helpUrl, useUiConfig } from '../shell/uiConfig';

export function Ipv6GuestReference({
  item,
  caps,
}: {
  item: NetworkPlacement;
  caps: SiteIpv6Caps | undefined;
}): React.ReactElement | null {
  const { t } = useTranslation();
  const shown = showsGuestReference(item, caps);
  const { helpBaseUrl } = useUiConfig(shown);
  if (!shown) return null;
  const ref = guestReference(item);
  if (!ref) return null;
  const copy = (text: string) => (
    <ClipboardCopy key={text} isCode isReadOnly variant="inline-compact" hoverTip={t('ipv6.guest.copy')} clickTip={t('ipv6.guest.copied')}>
      {text}
    </ClipboardCopy>
  );
  const row = (label: string, value: React.ReactNode) => (
    <DescriptionListGroup key={label}>
      <DescriptionListTerm>{label}</DescriptionListTerm>
      <DescriptionListDescription>{value}</DescriptionListDescription>
    </DescriptionListGroup>
  );
  const tr = ref.translation;
  const facts = (title: string, keys: string[]) =>
    keys.length ? (
      <section key={title} style={{ marginTop: '0.75rem' }}>
        <p>
          <strong>{title}</strong>
        </p>
        <ul style={{ paddingLeft: '1.25rem', listStyle: 'disc' }}>
          {keys.map((k) => (
            <li key={k}>{t(`ipv6.guest.fact.${k}`)}</li>
          ))}
        </ul>
      </section>
    ) : null;
  return (
    <ExpandableSection toggleText={t('ipv6.guest.title')} className="sc-mb">
      <p className="sc-text-muted">{t('ipv6.guest.help')}</p>
      <DescriptionList isCompact isHorizontal style={{ marginTop: '0.75rem' }}>
        {row(t('ipv6.guest.environment'), t(`ipv6.guest.environment_${ref.environment}`))}
        {ref.addressing && row(t('ipv6.addressing'), ref.addressing)}
        {ref.mtu !== undefined && row(t('ipv6.guest.mtu'), copy(String(ref.mtu)))}
        {row(
          t('ipv6.guest.translation'),
          [
            t(`ipv6.guest.translation_${ref.translationState}`),
            tr?.path ? `(${t(`ipv6.path_${tr.path}`, { defaultValue: tr.path })})` : '',
          ]
            .filter(Boolean)
            .join(' '),
        )}
        {tr && row(t('ipv6.guest.pref64'), copy(tr.pref64))}
        {tr && tr.resolvers.length > 0 && row(t('ipv6.guest.resolvers'), <>{tr.resolvers.map((r) => <div key={r}>{copy(r)}</div>)}</>)}
        {tr?.ipv4Source && row(t('ipv6.translationSource'), copy(tr.ipv4Source))}
      </DescriptionList>
      {facts(t('ipv6.guest.factsEnvironment'), ref.facts.environment)}
      {facts(t('ipv6.guest.factsResolvers'), ref.facts.resolvers)}
      {facts(t('ipv6.guest.factsClat'), ref.facts.clat)}
      {facts(t('ipv6.guest.factsEvery'), ref.facts.every)}
      <p className="sc-text-muted" style={{ marginTop: '0.75rem' }}>
        {helpBaseUrl ? (
          <a href={helpUrl(helpBaseUrl, GUEST_REFERENCE_DOC)} target="_blank" rel="noopener noreferrer">
            {t('ipv6.guest.docLink')}
          </a>
        ) : (
          t('ipv6.guest.docText')
        )}
      </p>
    </ExpandableSection>
  );
}
