import React from 'react';
import { Checkbox, FormGroup, FormHelperText, HelperText, HelperTextItem } from '@patternfly/react-core';
import { useTranslation } from '../i18n';
import type { PlacementUse } from '../types';

/** "Use for virtual machines" and "Use for router attachments" (OpenStack environments). */
export function PlacementUseFields({
  idPrefix,
  value,
  onChange,
  problem,
}: {
  idPrefix: string;
  value: Required<PlacementUse>;
  onChange: (next: Required<PlacementUse>) => void;
  problem?: 'needOne' | null;
}): React.ReactElement {
  const { t } = useTranslation();
  return (
    <FormGroup role="group" isStack label={t('attach.uses')} fieldId={`${idPrefix}-use`}>
      <Checkbox
        id={`${idPrefix}-use-vm`}
        label={t('attach.useVm')}
        description={t('attach.useVmHelp')}
        isChecked={value.virtualMachines}
        onChange={(_e, v) => onChange({ ...value, virtualMachines: v })}
      />
      <Checkbox
        id={`${idPrefix}-use-routers`}
        label={t('attach.useRouters')}
        description={t('attach.useRoutersHelp')}
        isChecked={value.routerAttachments}
        onChange={(_e, v) => onChange({ ...value, routerAttachments: v })}
      />
      {problem && (
        <FormHelperText>
          <HelperText>
            <HelperTextItem variant="error">{t('attach.useNeedOne')}</HelperTextItem>
          </HelperText>
        </FormHelperText>
      )}
    </FormGroup>
  );
}
