import React, { useState } from 'react';
import { Dropdown, DropdownItem, DropdownList, MenuToggle } from '@patternfly/react-core';
import { QuestionCircleIcon } from '@patternfly/react-icons';
import { useTranslation } from '../i18n';
import { useOptionalScope } from '../scope/ScopeProvider';
import { HELP_PAGES, helpUrl, useUiConfig } from './uiConfig';

/** The masthead "?" menu (design 5.7). Links come from the installation's help base URL. */
export function HelpMenu(): React.ReactElement {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const { helpBaseUrl } = useUiConfig();
  const platform = !!useOptionalScope()?.identity?.platform.admin;
  const pages = HELP_PAGES.filter((p) => !p.platformOnly || platform);
  return (
    <Dropdown
      isOpen={open}
      onOpenChange={setOpen}
      popperProps={{ position: 'right' }}
      toggle={(ref) => (
        <MenuToggle ref={ref} variant="plain" aria-label={t('shell.help.menu')} onClick={() => setOpen((o) => !o)} isExpanded={open}>
          <QuestionCircleIcon />
        </MenuToggle>
      )}
    >
      <DropdownList>
        {helpBaseUrl ? (
          pages.map((p) => (
            <DropdownItem key={p.id} to={helpUrl(helpBaseUrl, p.path)} target="_blank" rel="noopener noreferrer" isExternalLink>
              {t(p.labelKey)}
            </DropdownItem>
          ))
        ) : (
          <DropdownItem key="none" isDisabled description={t('shell.help.notConfiguredHint')}>
            {t('shell.help.notConfigured')}
          </DropdownItem>
        )}
      </DropdownList>
    </Dropdown>
  );
}
