import React from 'react';
import { Alert, Button, Modal, ModalBody, ModalFooter, ModalHeader, ModalVariant } from '@patternfly/react-core';
import { useTranslation } from '../i18n';
import { retryConfirmation } from '../status/install';

/**
 * The question before a new installation of a stopped cluster starts (Retry installation, or Save
 * on the Configuration tab): what happens to the stopped installation, what the cluster keeps, and
 * the node counts it installs with (`spec`: the cluster's spec, or the one about to be saved).
 */
export function RetryInstallationModal({
  isOpen,
  spec,
  busy = false,
  error,
  onConfirm,
  onCancel,
}: {
  isOpen: boolean;
  spec: unknown;
  busy?: boolean;
  error?: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}): React.ReactElement {
  const { t } = useTranslation();
  const c = retryConfirmation(t, spec);
  return (
    <Modal
      variant={ModalVariant.small}
      isOpen={isOpen}
      onClose={() => (!busy ? onCancel() : undefined)}
      aria-labelledby="sc-retry-install-title"
    >
      <ModalHeader title={c.title} labelId="sc-retry-install-title" titleIconVariant="warning" />
      <ModalBody>
        {error && <Alert variant="danger" isInline className="sc-mb" title={error} />}
        <p className="sc-mb">{c.body}</p>
        <p>{c.settings}</p>
      </ModalBody>
      <ModalFooter>
        <Button key="confirm" variant="primary" isLoading={busy} isDisabled={busy} onClick={onConfirm}>
          {c.confirm}
        </Button>
        <Button key="cancel" variant="link" isDisabled={busy} onClick={onCancel}>
          {t('common.cancel')}
        </Button>
      </ModalFooter>
    </Modal>
  );
}
