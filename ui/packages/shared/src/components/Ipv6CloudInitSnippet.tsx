/**
 * Copyable guest configuration (cloud-init) for a placement with an IPv6 subnet, built from the placement's status
 * only (no installation values in the template). Shown only where the placement's site reports IPv6: the same
 * capability check that gates the placement form's IPv6 inputs.
 *
 * On OpenShift Virtualization the tenant gives the VM its network data, which finds the VM's NIC by its MAC address:
 * the panel proposes a new locally administered address (or takes the tenant's) and shows three blocks that agree with
 * each other: the VM interface with that address, the VM's cloud-init volume (network data inline) and the Secret with
 * the user data that the volume references (KubeVirt refuses inline user data over 2048 bytes). Elsewhere the cloud
 * provides the network configuration and the panel shows the user data only.
 */
import React from 'react';
import {
  Button,
  ClipboardCopy,
  ExpandableSection,
  FormGroup,
  FormHelperText,
  HelperText,
  HelperTextItem,
  TextInput,
} from '@patternfly/react-core';
import { useTranslation } from '../i18n';
import type { NetworkPlacement } from '../types';
import { cloudInitSnippet, proposeMacAddress, showsCloudInitSnippet, type SiteIpv6Caps } from '../forms/ipv6';

export function Ipv6CloudInitSnippet({
  item,
  caps,
}: {
  item: NetworkPlacement;
  caps: SiteIpv6Caps | undefined;
}): React.ReactElement | null {
  const { t } = useTranslation();
  // a new address on every visit to the page: two VMs with the same MAC address on one network break each other
  const [mac, setMac] = React.useState(() => proposeMacAddress());
  if (!showsCloudInitSnippet(item, caps)) return null;
  const snippet = cloudInitSnippet(item, { macAddress: mac });
  if (!snippet) return null;
  // keyed by the text, so a new MAC address replaces what the copy field holds
  const copy = (text: string) => (
    <ClipboardCopy key={text} isCode isReadOnly variant="expansion" hoverTip={t('ipv6.cloudInit.copy')} clickTip={t('ipv6.cloudInit.copied')}>
      {text}
    </ClipboardCopy>
  );
  const fieldId = `cloud-init-mac-${item.metadata?.name ?? 'placement'}`;
  return (
    <ExpandableSection toggleText={t('ipv6.cloudInit.title')} className="sc-mb">
      <p className="sc-text-muted">{t('ipv6.cloudInit.help')}</p>
      {snippet.tenantNetworkData ? (
        <>
          <FormGroup label={t('ipv6.cloudInit.macAddress')} fieldId={fieldId} style={{ marginTop: '0.75rem' }}>
            <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center', maxWidth: '32rem' }}>
              <TextInput
                id={fieldId}
                value={mac}
                onChange={(_e, v) => setMac(v)}
                validated={snippet.macAddress ? 'default' : 'error'}
              />
              <Button variant="link" isInline onClick={() => setMac(proposeMacAddress())}>
                {t('ipv6.cloudInit.newMac')}
              </Button>
            </div>
            <FormHelperText>
              <HelperText>
                {snippet.macAddress ? (
                  <HelperTextItem>{t('ipv6.cloudInit.macAddressHelp')}</HelperTextItem>
                ) : (
                  <HelperTextItem variant="error">{t('ipv6.cloudInit.macAddressInvalid')}</HelperTextItem>
                )}
              </HelperText>
            </FormHelperText>
          </FormGroup>
          {snippet.vmInterface && (
            <>
              <p style={{ marginTop: '0.75rem' }}>
                <strong>{t('ipv6.cloudInit.vmInterface')}</strong>
              </p>
              <p className="sc-text-muted">{t('ipv6.cloudInit.vmInterfaceHelp')}</p>
              {copy(snippet.vmInterface)}
            </>
          )}
          {snippet.vmVolume && (
            <>
              <p style={{ marginTop: '0.75rem' }}>
                <strong>{t('ipv6.cloudInit.vmVolume')}</strong>
              </p>
              <p className="sc-text-muted">
                {t(snippet.networkDataInSecret ? 'ipv6.cloudInit.vmVolumeHelpNetworkInSecret' : 'ipv6.cloudInit.vmVolumeHelp')}
              </p>
              {copy(snippet.vmVolume)}
            </>
          )}
          {snippet.userDataSecret && (
            <>
              <p style={{ marginTop: '0.75rem' }}>
                <strong>{t('ipv6.cloudInit.userDataSecret')}</strong>
              </p>
              <p className="sc-text-muted">
                {[
                  t('ipv6.cloudInit.userDataSecretHelp', { name: snippet.secretName ?? '' }),
                  snippet.ignoreAutoDns ? t('ipv6.cloudInit.userDataDns') : '',
                  snippet.clat ? t('ipv6.cloudInit.userDataHelp') : '',
                ]
                  .filter(Boolean)
                  .join(' ')}
              </p>
              {copy(snippet.userDataSecret)}
            </>
          )}
        </>
      ) : (
        <p className="sc-text-muted">
          {t(item.spec?.backend?.kind === 'CloudOSO' ? 'ipv6.cloudInit.networkFromConfigDrive' : 'ipv6.cloudInit.networkFromCloud')}
        </p>
      )}
      {snippet.userData && !snippet.userDataSecret && (
        <>
          <p style={{ marginTop: '0.75rem' }}>
            <strong>{t('ipv6.cloudInit.userData')}</strong>
          </p>
          <p className="sc-text-muted">
            {[snippet.ignoreAutoDns ? t('ipv6.cloudInit.userDataDns') : '', snippet.clat ? t('ipv6.cloudInit.userDataHelp') : '']
              .filter(Boolean)
              .join(' ')}
          </p>
          {copy(snippet.userData)}
        </>
      )}
    </ExpandableSection>
  );
}
