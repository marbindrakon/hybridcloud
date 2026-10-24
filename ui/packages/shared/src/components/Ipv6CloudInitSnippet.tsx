/**
 * Copyable guest network configuration (cloud-init) for a placement with an IPv6 subnet, built from the
 * placement's status only (no installation values in the template). Shown only where the placement's site
 * reports IPv6: the same capability check that gates the placement form's IPv6 inputs.
 */
import React from 'react';
import { ClipboardCopy, ExpandableSection } from '@patternfly/react-core';
import { useTranslation } from '../i18n';
import type { NetworkPlacement } from '../types';
import { cloudInitSnippet, showsCloudInitSnippet, type SiteIpv6Caps } from '../forms/ipv6';

export function Ipv6CloudInitSnippet({
  item,
  caps,
}: {
  item: NetworkPlacement;
  caps: SiteIpv6Caps | undefined;
}): React.ReactElement | null {
  const { t } = useTranslation();
  if (!showsCloudInitSnippet(item, caps)) return null;
  const snippet = cloudInitSnippet(item);
  if (!snippet) return null;
  return (
    <ExpandableSection toggleText={t('ipv6.cloudInit.title')} className="sc-mb">
      <p className="sc-text-muted">{t('ipv6.cloudInit.help')}</p>
      <p>
        <strong>{t('ipv6.cloudInit.networkData')}</strong>
      </p>
      <ClipboardCopy isCode isReadOnly variant="expansion" hoverTip={t('ipv6.cloudInit.copy')} clickTip={t('ipv6.cloudInit.copied')}>
        {snippet.networkData}
      </ClipboardCopy>
      {snippet.userData && (
        <>
          <p style={{ marginTop: '0.75rem' }}>
            <strong>{t('ipv6.cloudInit.userData')}</strong>
          </p>
          <p className="sc-text-muted">{t('ipv6.cloudInit.userDataHelp')}</p>
          <ClipboardCopy isCode isReadOnly variant="expansion" hoverTip={t('ipv6.cloudInit.copy')} clickTip={t('ipv6.cloudInit.copied')}>
            {snippet.userData}
          </ClipboardCopy>
        </>
      )}
    </ExpandableSection>
  );
}
