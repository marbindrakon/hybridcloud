import React, { useMemo } from 'react';
import { Link } from 'react-router-dom';
import {
  Button,
  Masthead,
  MastheadBrand,
  MastheadContent,
  MastheadLogo,
  MastheadMain,
  MastheadToggle,
  PageToggleButton,
  Toolbar,
  ToolbarContent,
  ToolbarGroup,
  ToolbarItem,
} from '@patternfly/react-core';
import { MoonIcon, SunIcon, TopologyIcon } from '@patternfly/react-icons';
import { LanguageToggle, useTranslation } from '../i18n';
import { useTheme } from '../theme';
import { TenantScopeSelector } from '../scope/TenantScopeSelector';
import { AccountMenu } from './AccountMenu';
import { HelpMenu } from './HelpMenu';

function ThemeToggle(): React.ReactElement {
  const { mode, toggleTheme } = useTheme();
  const { t } = useTranslation();
  return (
    <Button
      variant="plain"
      aria-label={t('common.toggleTheme')}
      onClick={toggleTheme}
      icon={mode === 'dark' ? <SunIcon /> : <MoonIcon />}
    />
  );
}

export interface SovereignMastheadProps {
  brand: string;
  homeHref: string;
  /** Offer the platform scope in the selector (admin dashboard). */
  includePlatform?: boolean;
}

/**
 * PatternFly 6 masthead for the dashboards (Q2): nav toggle (the managed
 * sidebar closes below 1200 px), brand, tenant selector, Help, theme,
 * language and the account menu. No notification bell: nothing notifies.
 */
export function SovereignMasthead({ brand, homeHref, includePlatform = false }: SovereignMastheadProps): React.ReactElement {
  const { t } = useTranslation();
  const BrandLink = useMemo(
    () =>
      function BrandLink(props: Record<string, unknown>) {
        return <Link {...props} to={homeHref} />;
      },
    [homeHref],
  );
  return (
    <Masthead className="sc-pf-masthead">
      <MastheadMain>
        <MastheadToggle>
          <PageToggleButton isHamburgerButton aria-label={t('common.globalNav')} />
        </MastheadToggle>
        <MastheadBrand>
          <MastheadLogo component={BrandLink} className="sc-masthead-brand">
            <span className="sc-masthead-brand__mark" aria-hidden>
              <TopologyIcon />
            </span>
            <span>{brand}</span>
          </MastheadLogo>
        </MastheadBrand>
      </MastheadMain>
      <MastheadContent>
        <Toolbar isFullHeight isStatic>
          <ToolbarContent>
            <ToolbarItem>
              <TenantScopeSelector includePlatform={includePlatform} />
            </ToolbarItem>
            <ToolbarGroup align={{ default: 'alignEnd' }} gap={{ default: 'gapNone', md: 'gapMd' }}>
              <ToolbarItem>
                <HelpMenu />
              </ToolbarItem>
              <ToolbarItem>
                <ThemeToggle />
              </ToolbarItem>
              <ToolbarItem>
                <LanguageToggle isCompact />
              </ToolbarItem>
              <ToolbarItem>
                <AccountMenu />
              </ToolbarItem>
            </ToolbarGroup>
          </ToolbarContent>
        </Toolbar>
      </MastheadContent>
    </Masthead>
  );
}
