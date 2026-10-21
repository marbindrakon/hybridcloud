import React from 'react';
import { Page, PageSection, PageSidebar, PageSidebarBody, SkipToContent } from '@patternfly/react-core';
import { useTranslation } from '../i18n';
import type { Host } from '../nav/types';
import { ShellNav } from './ShellNav';
import { SovereignMasthead } from './SovereignMasthead';

export const MAIN_CONTENT_ID = 'sc-main-content';

export interface DashboardShellProps {
  host: Host;
  brand: string;
  homeHref: string;
  includePlatform?: boolean;
  children?: React.ReactNode;
}

/** Page frame of both dashboards: masthead, registry sidebar, skip link, main landmark. */
export function DashboardShell({ host, brand, homeHref, includePlatform, children }: DashboardShellProps): React.ReactElement {
  const { t } = useTranslation();
  const sidebar = (
    <PageSidebar id={`${host}-sidebar`}>
      <PageSidebarBody>
        <ShellNav host={host} />
      </PageSidebarBody>
    </PageSidebar>
  );
  return (
    <Page
      className="sc-standalone-shell"
      masthead={<SovereignMasthead brand={brand} homeHref={homeHref} includePlatform={includePlatform} />}
      sidebar={sidebar}
      isManagedSidebar
      skipToContent={<SkipToContent href={`#${MAIN_CONTENT_ID}`}>{t('shell.skipToContent')}</SkipToContent>}
      mainContainerId={MAIN_CONTENT_ID}
    >
      <PageSection isFilled className="sc-page-section sc-page-section--dashboard">
        <div className="sc-page">{children}</div>
      </PageSection>
    </Page>
  );
}
