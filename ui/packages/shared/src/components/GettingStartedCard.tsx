import React, { useState } from 'react';
import { Link } from 'react-router-dom';
import { Button, Card, CardBody, CardHeader, CardTitle } from '@patternfly/react-core';
import { TimesIcon } from '@patternfly/react-icons';
import { useTranslation } from '../i18n';
import { useNav } from '../nav/useNav';

const STORAGE_KEY = 'sc.gettingStarted.dismissed';

function readDismissed(): boolean {
  try {
    return window.localStorage.getItem(STORAGE_KEY) === '1';
  } catch {
    return false;
  }
}

function writeDismissed(): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, '1');
  } catch {
    /* per-viewer convenience only */
  }
}

/**
 * The five-minute explanation on the tenant Home (Q13, UX-022): what Sovereign
 * Cloud gives you, in four steps. Dismissible per viewer (browser storage).
 */
export function GettingStartedCard(): React.ReactElement | null {
  const { t } = useTranslation();
  const nav = useNav();
  const [dismissed, setDismissed] = useState(readDismissed);
  if (dismissed) return null;
  const steps: Array<{ key: string; href?: string }> = [
    { key: 'gettingStarted.step1', href: nav.createPathFor('hybridnetwork') },
    { key: 'gettingStarted.step2', href: nav.createPathFor('cloudvirt') },
    { key: 'gettingStarted.step3', href: nav.pathFor('env-vm') },
    { key: 'gettingStarted.step4' },
  ];
  return (
    <Card isCompact className="sc-mb sc-getting-started">
      <CardHeader
        actions={{
          actions: (
            <Button
              variant="plain"
              aria-label={t('gettingStarted.dismiss')}
              icon={<TimesIcon />}
              onClick={() => {
                writeDismissed();
                setDismissed(true);
              }}
            />
          ),
          hasNoOffset: false,
        }}
      >
        <CardTitle>{t('gettingStarted.title')}</CardTitle>
      </CardHeader>
      <CardBody>
        <p>{t('gettingStarted.intro')}</p>
        <ol>
          {steps.map((s) => (
            <li key={s.key}>{s.href ? <Link to={s.href}>{t(s.key)}</Link> : t(s.key)}</li>
          ))}
        </ol>
      </CardBody>
    </Card>
  );
}
