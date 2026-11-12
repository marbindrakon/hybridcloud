import React, { useState } from 'react';
import { Alert, Button, Modal, ModalBody, ModalFooter, ModalHeader, ModalVariant } from '@patternfly/react-core';
import { useTranslation } from '../i18n';
import { updateDashboardResource } from '../hooks/k8s';
import { errorText } from '../access/errorText';
import { GatedButton } from '../access/GatedButton';
import { pushToast } from './ToastHost';
import type { HybridNetwork } from '../types';

/**
 * "Allow platform access": turns spec.platformAccess on for a network after a confirmation that
 * quotes what it allows. Shown where a network cannot host a cluster because the access is off.
 */
export function AllowPlatformAccessButton({
  network,
  onDone,
  variant = 'secondary',
  isInline,
}: {
  network: HybridNetwork;
  onDone?: () => void;
  variant?: 'secondary' | 'link';
  isInline?: boolean;
}): React.ReactElement {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const name = network.metadata.name;
  const namespace = network.metadata.namespace ?? '';
  const titleId = `allow-platform-access-${name}`;

  const confirm = async () => {
    setSaving(true);
    setError(null);
    try {
      // The curated route replaces the whole spec: send it with the one field changed.
      await updateDashboardResource('HybridNetwork', name, namespace, { spec: { ...(network.spec ?? {}), platformAccess: true } });
      pushToast({ variant: 'success', title: t('clusterNet.allowDone', { network: name }) });
      setOpen(false);
      onDone?.();
    } catch (err) {
      const txt = errorText(err, t, { action: 'action' });
      setError([txt.title, txt.body].filter(Boolean).join(' '));
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <GatedButton kind="HybridNetwork" verb="update" namespace={namespace} variant={variant} isInline={isInline} onClick={() => setOpen(true)}>
        {t('clusterNet.allowPlatformAccess')}
      </GatedButton>
      {open && (
        <Modal variant={ModalVariant.small} isOpen onClose={() => setOpen(false)} aria-labelledby={titleId}>
          <ModalHeader title={t('clusterNet.allowTitle', { network: name })} labelId={titleId} />
          <ModalBody>
            <p>{t('clusterNet.allowBody')}</p>
            {error && (
              <Alert variant="danger" isInline title={t('clusterNet.allowFailed')} className="sc-mb" style={{ marginTop: '1rem' }}>
                {error}
              </Alert>
            )}
          </ModalBody>
          <ModalFooter>
            <Button variant="primary" onClick={() => void confirm()} isLoading={saving} isDisabled={saving}>
              {t('clusterNet.allowConfirm')}
            </Button>
            <Button variant="link" onClick={() => setOpen(false)}>
              {t('common.cancel')}
            </Button>
          </ModalFooter>
        </Modal>
      )}
    </>
  );
}
