import React from 'react';
import { Link } from 'react-router-dom';
import { Nav, NavGroup, NavItem, NavList } from '@patternfly/react-core';
import { useTranslation } from '../i18n';
import { KindIcon } from '../icons';
import { useNav } from '../nav/useNav';
import type { Host } from '../nav/types';

/** Sidebar navigation of a dashboard host, rendered from the registry. */
export function ShellNav({ host, ariaLabel }: { host: Host; ariaLabel?: string }): React.ReactElement {
  const { t } = useTranslation();
  const { groups } = useNav(host);
  return (
    <Nav aria-label={ariaLabel ?? t('shell.nav')}>
      {groups.map((g) =>
        g.section === 'home' ? (
          <NavList key={g.section}>
            {g.items.map((i) => (
              <NavItem key={i.entry.id} isActive={i.active}>
                <Link to={i.href} className="sc-nav-link" aria-current={i.active ? 'page' : undefined}>
                  {t(i.entry.labelKey)}
                </Link>
              </NavItem>
            ))}
          </NavList>
        ) : (
          <NavGroup key={g.section} title={t(g.labelKey)}>
            {g.items.map((i) => (
              <NavItem key={i.entry.id} isActive={i.active}>
                <Link to={i.href} className="sc-nav-link" aria-current={i.active ? 'page' : undefined}>
                  {i.entry.kinds?.[0] && (
                    <span className="sc-nav-link__icon" aria-hidden>
                      <KindIcon kind={i.entry.kinds[0]} size="sm" />
                    </span>
                  )}
                  {t(i.entry.labelKey)}
                </Link>
              </NavItem>
            ))}
          </NavGroup>
        ),
      )}
    </Nav>
  );
}
