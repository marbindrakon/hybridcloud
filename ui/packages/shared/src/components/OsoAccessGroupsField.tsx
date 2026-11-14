import React from 'react';
import { FormGroup, FormHelperText, HelperText, HelperTextItem } from '@patternfly/react-core';
import { useTranslation } from '../i18n';
import { RbacMultiSelect } from './RbacMultiSelect';

export interface OsoAccessGroupsFieldProps {
  /** spec.toolRbac.editRbac */
  editRbac: string[];
  /** spec.toolRbac.viewRbac */
  viewRbac: string[];
  onEditChange: (next: string[]) => void;
  onViewChange: (next: string[]) => void;
  /** Access groups of the tenant */
  options: { value: string; label: string }[];
  /** false: the chosen site offers no sign-in with the platform identity (a hint); undefined: not known */
  siteSignIn?: boolean;
  isDisabled?: boolean;
}

/**
 * "Groups that can use this environment" of an OpenStack environment: the access groups whose members can
 * sign in to the site's dashboard, with two lists (create and change, read only). The form writes them to
 * spec.toolRbac; the platform's job grants them on the site.
 */
export function OsoAccessGroupsField({
  editRbac,
  viewRbac,
  onEditChange,
  onViewChange,
  options,
  siteSignIn,
  isDisabled,
}: OsoAccessGroupsFieldProps): React.ReactElement {
  const { t } = useTranslation();
  return (
    <FormGroup role="group" label={t('fields.osoAccessGroups')} fieldId="oso-edit-rbac" isStack>
      <RbacMultiSelect
        id="oso-edit-rbac"
        label={t('fields.osoEditRbac')}
        value={editRbac}
        onChange={onEditChange}
        options={options}
        isDisabled={isDisabled}
      />
      <RbacMultiSelect
        id="oso-view-rbac"
        label={t('fields.osoViewRbac')}
        value={viewRbac}
        onChange={onViewChange}
        options={options}
        isDisabled={isDisabled}
      />
      <FormHelperText>
        <HelperText>
          <HelperTextItem>{t('fields.osoAccessGroupsHelp')}</HelperTextItem>
          {siteSignIn === false && <HelperTextItem variant="warning">{t('fields.osoSiteNoSignIn')}</HelperTextItem>}
        </HelperText>
      </FormHelperText>
    </FormGroup>
  );
}
