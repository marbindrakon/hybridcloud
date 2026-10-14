import '../consoleK8sBootstrap';
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Label,
  Spinner,
  PageSection,
} from '@patternfly/react-core';
import { PlusCircleIcon, SyncIcon } from '@patternfly/react-icons';
import { Table, Thead, Tr, Th, Tbody, Td } from '@patternfly/react-table';
import { useNavigate } from 'react-router-dom';
import { consoleFetch } from '@openshift-console/dynamic-plugin-sdk';
import {
  K8sResource,
  PageHeader,
  useK8sResourceList,
  useTranslation,
} from '@hybridsovereign/shared';
import '@hybridsovereign/shared/styles/openshift.css';

const UIHEALTH_PROBE_URL =
  '/api/proxy/plugin/sovereign-admin-plugin/uihealth/api/uihealth/probe';

type ProbeResult = {
  status: 'live' | 'unreachable' | 'pending';
  httpStatus?: number;
  error?: string;
  checkedAt?: string;
};

type ProbeApiRow = {
  name: string;
  url?: string;
  status?: number;
  healthy?: boolean;
  error?: string;
  checkedAt?: string;
};

const AdminUIHealthPage: React.FC = () => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { items, loading, error, refresh } = useK8sResourceList<K8sResource>('UIHealthChecker', {
    namespace: 'sovereign-cloud',
  });
  const [results, setResults] = useState<Record<string, ProbeResult>>({});
  const [running, setRunning] = useState(false);
  const [probeError, setProbeError] = useState<string | null>(null);
  const autoProbedRef = useRef(false);

  const runChecks = useCallback(
    async (targetsOverride?: K8sResource[]) => {
      const source = targetsOverride ?? items;
      setRunning(true);
      setProbeError(null);
      const targets = source.map((item) => {
        const spec = (item.spec ?? {}) as {
          url?: string;
          timeoutSeconds?: number;
          expectedStatus?: number;
        };
        return {
          name: item.metadata.name,
          url: spec.url ?? '',
          timeoutSeconds: spec.timeoutSeconds ?? 10,
          expectedStatus: spec.expectedStatus ?? 200,
        };
      });

      try {
        const resp = await consoleFetch(UIHEALTH_PROBE_URL, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ targets }),
        });
        if (!resp.ok) {
          const body = await resp.json().catch(() => ({}));
          throw new Error(
            typeof body?.message === 'string' ? body.message : `probe failed (${resp.status})`,
          );
        }
        const data = (await resp.json()) as { results?: ProbeApiRow[] };
        const next: Record<string, ProbeResult> = {};
        for (const row of data.results ?? []) {
          next[row.name] = row.healthy
            ? { status: 'live', httpStatus: row.status, checkedAt: row.checkedAt }
            : {
                status: 'unreachable',
                httpStatus: row.status || undefined,
                error: row.error || (row.status ? `HTTP ${row.status}` : 'unreachable'),
                checkedAt: row.checkedAt,
              };
        }
        setResults(next);
      } catch (err) {
        setProbeError(err instanceof Error ? err.message : 'probe failed');
      } finally {
        setRunning(false);
      }
    },
    [items],
  );

  const refreshAndProbe = useCallback(() => {
    autoProbedRef.current = false;
    refresh();
  }, [refresh]);

  useEffect(() => {
    if (loading || items.length === 0 || autoProbedRef.current) return;
    autoProbedRef.current = true;
    void runChecks(items);
  }, [loading, items, runChecks]);

  const summary = useMemo(() => {
    const values = Object.values(results);
    return {
      live: values.filter((v) => v.status === 'live').length,
      unreachable: values.filter((v) => v.status === 'unreachable').length,
    };
  }, [results]);

  return (
    <PageSection className="sc-console-page">
      <div className="sc-page">
        <PageHeader
          title={t('nav.uiHealth')}
          subtitle={t('pages.uiHealthSubtitle')}
          breadcrumbs={[{ label: t('nav.sovereignCloud') }, { label: t('nav.uiHealth') }]}
          actions={
            <>
              <Button
                variant="secondary"
                icon={<SyncIcon />}
                onClick={() => void refreshAndProbe()}
                isDisabled={loading || running}
              >
                {t('common.refresh')}
              </Button>
              <Button
                variant="primary"
                onClick={() => void runChecks()}
                isDisabled={running || items.length === 0}
              >
                {running ? t('pages.uiHealthRunning') : t('pages.uiHealthRun')}
              </Button>
              <Button
                variant="secondary"
                icon={<PlusCircleIcon />}
                onClick={() => navigate('/hybridsovereign/create/uihealthchecker')}
              >
                {t('common.create')}
              </Button>
            </>
          }
        />

        {error && (
          <Alert variant="warning" isInline title={t('common.k8sUnavailable')} className="sc-mb">
            {error.message}
          </Alert>
        )}

        {probeError && (
          <Alert variant="danger" isInline title={t('pages.uiHealthProbeFailed')} className="sc-mb">
            {probeError}
          </Alert>
        )}

        <Alert variant="info" isInline title={t('pages.uiHealthPodNote')} className="sc-mb">
          {t('pages.uiHealthPodNoteBody')}
        </Alert>

        {Object.keys(results).length > 0 && (
          <p className="sc-mb">
            {t('pages.uiHealthSummary', {
              live: summary.live,
              unreachable: summary.unreachable,
            })}
          </p>
        )}

        {loading && items.length === 0 ? (
          <Spinner />
        ) : (
          <div className="sc-table-wrap">
            <Table aria-label={t('nav.uiHealth')} variant="compact">
              <Thead>
                <Tr>
                  <Th>{t('common.name')}</Th>
                  <Th>{t('fields.group')}</Th>
                  <Th>{t('fields.url')}</Th>
                  <Th>{t('pages.uiHealthLive')}</Th>
                </Tr>
              </Thead>
              <Tbody>
                {items.length === 0 ? (
                  <Tr>
                    <Td colSpan={4}>{t('pages.uiHealthEmpty')}</Td>
                  </Tr>
                ) : (
                  items.map((item) => {
                    const spec = (item.spec ?? {}) as {
                      url?: string;
                      group?: string;
                      displayName?: string;
                    };
                    const probe = results[item.metadata.name];
                    return (
                      <Tr key={item.metadata.name}>
                        <Td>
                          <a
                            href={`/hybridsovereign/networking/uihealth/${encodeURIComponent(item.metadata.name)}`}
                          >
                            {spec.displayName || item.metadata.name}
                          </a>
                        </Td>
                        <Td>{spec.group || '—'}</Td>
                        <Td>
                          {spec.url ? (
                            <a href={spec.url} target="_blank" rel="noopener noreferrer">
                              {spec.url}
                            </a>
                          ) : (
                            '—'
                          )}
                        </Td>
                        <Td>
                          {!probe ? (
                            <Label color="grey">
                              {running ? t('pages.uiHealthRunning') : t('pages.uiHealthNotRun')}
                            </Label>
                          ) : probe.status === 'live' ? (
                            <Label color="green">
                              {t('pages.uiHealthLive')}
                              {probe.httpStatus ? ` (${probe.httpStatus})` : ''}
                            </Label>
                          ) : (
                            <Label color="red" title={probe.error}>
                              {t('pages.uiHealthUnreachable')}
                            </Label>
                          )}
                        </Td>
                      </Tr>
                    );
                  })
                )}
              </Tbody>
            </Table>
          </div>
        )}
      </div>
    </PageSection>
  );
};

export default AdminUIHealthPage;
