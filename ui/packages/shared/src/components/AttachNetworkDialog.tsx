import React, { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Alert,
  Button,
  ExpandableSection,
  Form,
  FormGroup,
  FormHelperText,
  FormSelect,
  FormSelectOption,
  HelperText,
  HelperTextItem,
  Modal,
  ModalBody,
  ModalFooter,
  ModalHeader,
  ModalVariant,
  TextInput,
} from '@patternfly/react-core';
import { useTranslation } from '../i18n';
import { createDashboardResource, useK8sResourceList } from '../hooks/k8s';
import { errorText } from '../access/errorText';
import { GatedButton } from '../access/GatedButton';
import { fieldErrorsFrom, causeLines } from '../forms/apiErrors';
import { attachBlock } from '../forms/attachable';
import { useRouteFor } from '../nav/useNav';
import {
  isPinnedIpv4Prefix,
  prefixWithin,
  PLACEMENT_BACKEND_KINDS,
  PLACEMENT_IPV4_DEFAULT,
  PLACEMENT_IPV4_MAX,
  PLACEMENT_IPV4_MAX_AWS,
  PLACEMENT_IPV4_MIN,
} from '../forms/hybridFabricDefaults';
import type { HybridNetwork, K8sResource, NetworkPlacement, NetworkPlacementBackendKind } from '../types';
import { kindTerm } from '../i18n/vocabulary';
import { useSiteIpv6Caps } from '../hooks/ipv6';
import {
  buildPlacementIpv6,
  environmentSiteName,
  isPinnedIpv6Prefix,
  networkHasIpv4,
  networkIpv6Mode,
  placementFamilies,
  placementIpv6Offer,
} from '../forms/ipv6';
import {
  INITIAL_PLACEMENT_IPV6,
  PlacementIpv6Fields,
  effectiveIpv6Length,
  placementIpv6NoteKey,
  type PlacementIpv6State,
} from '../forms/PlacementIpv6Fields';

export interface AttachEnvironmentRef {
  kind: NetworkPlacementBackendKind;
  name: string;
}

export interface AttachNetworkDialogProps {
  isOpen: boolean;
  /** Tenant namespace holding the network, the environment and the placement */
  namespace: string;
  /** Fixed environment (opened from an environment page or a map cell) */
  environment?: AttachEnvironmentRef;
  /** Fixed network (opened from a network page or a map cell) */
  network?: string;
  onClose: () => void;
  /** Called with the created placement (the parent refreshes and polls) */
  onAttached?: (placement: NetworkPlacement) => void;
}

/** Placement object name: <network>-<environment>, at most 63 characters, no trailing hyphen. */
export function defaultPlacementName(network: string, environment: string): string {
  const raw = `${network}-${environment}`.toLowerCase().replace(/[^a-z0-9-]/g, '-');
  return raw.slice(0, 63).replace(/-+$/, '');
}

const NAME_RE = /^[a-z0-9]([-a-z0-9]*[a-z0-9])?$/;
const DRY_RUN_DELAY_MS = 600;

const ENV_WHAT: Record<NetworkPlacementBackendKind, string> = {
  CloudVirt: 'attach.whatVirt',
  CloudOSO: 'attach.whatOpenstack',
  CloudAWS: 'attach.whatAws',
  CloudGCP: 'attach.whatGcp',
};

function envKey(e: AttachEnvironmentRef | undefined): string {
  return e ? `${e.kind}::${e.name}` : '';
}

function parseEnvKey(v: string): AttachEnvironmentRef | undefined {
  const [kind, name] = v.split('::');
  if (!kind || !name || !(PLACEMENT_BACKEND_KINDS as string[]).includes(kind)) return undefined;
  return { kind: kind as NetworkPlacementBackendKind, name };
}

/**
 * The one way to attach a network to an environment (OD25): it creates the
 * NetworkPlacement. Owned by the environment page, opened preselected from the
 * network page and from the network map. The address block size is bounded by
 * the environment type (the API's limits); a pinned prefix is used exactly or
 * waits while another placement holds it. A server dry run checks the exact
 * object before Create is offered.
 *
 * Only attachable pairs are offered (GT-F3): an OpenShift Virtualization
 * environment takes a network only when one of its namespaces names it; the
 * other networks (or environments) are shown greyed with the reason and a link
 * to the environment's Networks tab.
 */
export function AttachNetworkDialog({
  isOpen,
  namespace,
  environment: fixedEnv,
  network: fixedNetwork,
  onClose,
  onAttached,
}: AttachNetworkDialogProps): React.ReactElement | null {
  const { t } = useTranslation();
  const routeFor = useRouteFor();
  const enabled = isOpen && !!namespace;
  const networks = useK8sResourceList<HybridNetwork>('HybridNetwork', { namespace, enabled: enabled && !fixedNetwork });
  const fixedNetworkObj = useK8sResourceList<HybridNetwork>('HybridNetwork', { namespace, enabled: enabled && !!fixedNetwork });
  const placements = useK8sResourceList<NetworkPlacement>('NetworkPlacement', { namespace, enabled });
  // CloudVirts are read also for a fixed environment: their namespaces say which networks they take.
  const cloudvirts = useK8sResourceList<K8sResource>('CloudVirt', {
    namespace,
    enabled: enabled && (!fixedEnv || fixedEnv.kind === 'CloudVirt'),
  });
  // The other environment kinds: listed to choose from, or for the fixed environment's site (IPv6 offer).
  const cloudosos = useK8sResourceList<K8sResource>('CloudOSO', { namespace, enabled: enabled && (!fixedEnv || fixedEnv.kind === 'CloudOSO') });
  const cloudawss = useK8sResourceList<K8sResource>('CloudAWS', { namespace, enabled: enabled && (!fixedEnv || fixedEnv.kind === 'CloudAWS') });
  const cloudgcps = useK8sResourceList<K8sResource>('CloudGCP', { namespace, enabled: enabled && (!fixedEnv || fixedEnv.kind === 'CloudGCP') });
  const siteCaps = useSiteIpv6Caps(namespace, { enabled });

  const [networkName, setNetworkName] = useState(fixedNetwork ?? '');
  const [env, setEnv] = useState<AttachEnvironmentRef | undefined>(fixedEnv);
  const [prefixLength, setPrefixLength] = useState(String(PLACEMENT_IPV4_DEFAULT));
  const [pinned, setPinned] = useState('');
  const [v6, setV6] = useState<PlacementIpv6State>(INITIAL_PLACEMENT_IPV6);
  const [name, setName] = useState('');
  const [nameTouched, setNameTouched] = useState(false);
  const [checking, setChecking] = useState(false);
  const [checkedKey, setCheckedKey] = useState<string | null>(null);
  const [checkError, setCheckError] = useState<unknown>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<unknown>(null);

  // Reset when the dialog opens for another network or environment.
  useEffect(() => {
    if (!isOpen) return;
    setNetworkName(fixedNetwork ?? '');
    setEnv(fixedEnv);
    setPrefixLength(String(PLACEMENT_IPV4_DEFAULT));
    setPinned('');
    setV6(INITIAL_PLACEMENT_IPV6);
    setName('');
    setNameTouched(false);
    setCheckedKey(null);
    setCheckError(null);
    setSubmitError(null);
    setWarnings([]);
  }, [isOpen, fixedNetwork, fixedEnv?.kind, fixedEnv?.name]); // eslint-disable-line react-hooks/exhaustive-deps

  const envOptions = useMemo(() => {
    const of = (kind: NetworkPlacementBackendKind, items: K8sResource[]) =>
      items.map((i) => ({
        value: envKey({ kind, name: i.metadata.name }),
        kind,
        name: i.metadata.name,
        item: i,
        label: `${kindTerm(t, kind)} ${i.metadata.name}`,
      }));
    if (fixedEnv) return [];
    return [
      ...of('CloudVirt', cloudvirts.items),
      ...of('CloudOSO', cloudosos.items),
      ...of('CloudAWS', cloudawss.items),
      ...of('CloudGCP', cloudgcps.items),
    ];
  }, [fixedEnv, cloudvirts.items, cloudosos.items, cloudawss.items, cloudgcps.items, t]);

  // The chosen environment's object (CloudVirt only: its namespaces decide which networks it takes).
  const envObj = env?.kind === 'CloudVirt' ? cloudvirts.items.find((i) => i.metadata.name === env.name) : undefined;
  const envUnknown = env?.kind === 'CloudVirt' && !envObj && cloudvirts.loading;
  const envLabel = env ? `${kindTerm(t, env.kind)} ${env.name}` : '';
  const networksHref = (ref: AttachEnvironmentRef): string | undefined => {
    const href = routeFor(ref.kind, ref.name);
    return href ? `${href}?tab=networks` : undefined;
  };
  const networkChoices = (fixedNetwork ? [] : networks.items).map((n) => ({
    name: n.metadata.name,
    block: env ? attachBlock(env.kind, envObj, n.metadata.name) : null,
  }));
  const greyedNetworks = networkChoices.filter((n) => n.block);
  const envChoices = envOptions.map((o) => ({ ...o, block: networkName ? attachBlock(o.kind, o.item, networkName) : null }));
  const greyedEnvs = envChoices.filter((o) => o.block);
  const pairBlock = env && networkName ? attachBlock(env.kind, envObj, networkName) : null;

  const chosenNetwork = (fixedNetwork ? fixedNetworkObj.items : networks.items).find((n) => n.metadata.name === networkName);
  const space = (chosenNetwork?.status?.addressSpace ?? []).filter((a) => a.family === 4);
  const spacePrefixes = space.length ? space.map((a) => a.prefix) : (chosenNetwork?.spec?.prefixes ?? []).filter((p) => !p.includes(':'));
  const space6 = [
    ...(chosenNetwork?.status?.addressSpace ?? []).filter((a) => Number(a.family) === 6).map((a) => a.prefix),
    ...(chosenNetwork?.spec?.prefixes ?? []).filter((p) => p.includes(':')),
  ];

  // IPv6: only where the environment's site reports it and the network has IPv6 (the API stays the authority).
  const envItems: Record<NetworkPlacementBackendKind, K8sResource[]> = {
    CloudVirt: cloudvirts.items,
    CloudOSO: cloudosos.items,
    CloudAWS: cloudawss.items,
    CloudGCP: cloudgcps.items,
  };
  const envResource = env ? envItems[env.kind].find((i) => i.metadata.name === env.name) : undefined;
  const v6Offer = placementIpv6Offer({
    kind: env?.kind ?? 'CloudVirt',
    caps: siteCaps.capsFor(environmentSiteName(envResource)),
    networkMode: networkIpv6Mode(chosenNetwork).mode,
  });
  const hasIpv4Space = !chosenNetwork || networkHasIpv4(chosenNetwork);
  const families = placementFamilies({
    offer: v6Offer,
    networkHasIpv4: hasIpv4Space,
    includeIpv4: v6.includeIpv4,
    includeIpv6: v6.includeIpv6,
  });
  const v6Pinned = v6.pinned.trim();
  const v6PinnedOk = !v6Pinned || isPinnedIpv6Prefix(v6Pinned, v6Offer.lengths, space6);
  const v6Spec = families.ipv6
    ? buildPlacementIpv6({ kind: env?.kind ?? 'CloudVirt', source: v6.source, prefixLength: effectiveIpv6Length(v6Offer, v6), pinned: v6Pinned })
    : null;
  // The blockers list names a family problem; otherwise one line says why there is no IPv6 input.
  const v6Note = env && chosenNetwork && !families.block ? placementIpv6NoteKey(v6Offer, undefined) : null;
  const maxLength = env?.kind === 'CloudAWS' ? PLACEMENT_IPV4_MAX_AWS : PLACEMENT_IPV4_MAX;
  const lengths = Array.from({ length: maxLength - PLACEMENT_IPV4_MIN + 1 }, (_v, i) => PLACEMENT_IPV4_MIN + i);
  useEffect(() => {
    if (Number(prefixLength) > maxLength) setPrefixLength(String(Math.min(PLACEMENT_IPV4_DEFAULT, maxLength)));
  }, [maxLength, prefixLength]);

  const effectiveName = nameTouched ? name.trim() : networkName && env ? defaultPlacementName(networkName, env.name) : '';
  const pinnedText = pinned.trim();
  const pinnedOk =
    !families.ipv4 || !pinnedText || (isPinnedIpv4Prefix(pinnedText, maxLength) && (!spacePrefixes.length || prefixWithin(pinnedText, spacePrefixes)));
  const nameOk = !!effectiveName && effectiveName.length <= 63 && NAME_RE.test(effectiveName);
  const existing = placements.items.find(
    (p) => p.spec?.network === networkName && p.spec?.backend?.kind === env?.kind && p.spec?.backend?.name === env?.name,
  );

  const blockers: string[] = [];
  if (!networkName) blockers.push(t('attach.chooseNetwork'));
  if (!env) blockers.push(t('attach.chooseEnvironment'));
  if (existing) blockers.push(t('attach.alreadyAttached', { name: existing.metadata.name }));
  if (pairBlock) blockers.push(t('attach.notAttachable', { network: networkName, environment: envLabel }));
  if (envUnknown) blockers.push(t('form.checking'));
  if (!pinnedOk) blockers.push(t('attach.pinnedInvalid', { max: maxLength }));
  if (networkName && env && !nameOk) blockers.push(t('attach.nameInvalid'));
  if (env && chosenNetwork && families.block) blockers.push(t(placementIpv6NoteKey(v6Offer, families.block) ?? 'attach.needFamily'));
  if (families.ipv6 && !v6PinnedOk) blockers.push(t('attach.ipv6PinnedInvalid', { lengths: v6Offer.lengths.map((l) => `/${l}`).join(', ') }));

  const body = useMemo(() => {
    if (!networkName || !env || !nameOk || !pinnedOk || existing || pairBlock || envUnknown) return null;
    if (families.block || (families.ipv6 && (!v6Spec || !v6PinnedOk))) return null;
    return {
      name: effectiveName,
      namespace,
      spec: {
        network: networkName,
        backend: { kind: env.kind, name: env.name },
        ...(families.ipv4 ? { ipv4: pinnedText ? { prefix: pinnedText } : { prefixLength: Number(prefixLength) } } : {}),
        ...(families.ipv6 && v6Spec ? { ipv6: v6Spec } : {}),
      },
    };
  }, [networkName, env, nameOk, pinnedOk, existing, pairBlock, envUnknown, effectiveName, namespace, pinnedText, prefixLength, families.block, families.ipv4, families.ipv6, v6Spec, v6PinnedOk]); // eslint-disable-line react-hooks/exhaustive-deps
  const bodyKey = body ? JSON.stringify(body) : null;

  // Server dry run of the exact object (debounced); Create is offered only after it passed.
  useEffect(() => {
    if (!isOpen || !body || !bodyKey) return;
    let cancelled = false;
    setChecking(true);
    const id = setTimeout(() => {
      createDashboardResource('NetworkPlacement', body, namespace, { dryRun: true, onWarnings: (w) => !cancelled && setWarnings(w) })
        .then(() => {
          if (!cancelled) {
            setCheckedKey(bodyKey);
            setCheckError(null);
          }
        })
        .catch((err) => {
          if (!cancelled) {
            setCheckedKey(null);
            setCheckError(err);
          }
        })
        .finally(() => {
          if (!cancelled) setChecking(false);
        });
    }, DRY_RUN_DELAY_MS);
    return () => {
      cancelled = true;
      clearTimeout(id);
    };
  }, [isOpen, bodyKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const fieldMap = { 'spec.ipv4.prefix': 'pinned', 'spec.ipv4': 'size', 'metadata.name': 'name', 'spec.network': 'network', 'spec.backend': 'environment' };
  const shownError = submitError ?? checkError;
  const fieldErrors = fieldErrorsFrom(shownError, fieldMap);
  const errorTexts = shownError ? errorText(shownError, t, { kindLabel: t('kindsOne.NetworkPlacement'), action: 'action' }) : null;
  const canCreate = !!body && checkedKey === bodyKey && !checking && !submitting;

  const create = async () => {
    if (!body || !canCreate) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      const created = (await createDashboardResource('NetworkPlacement', body, namespace, {
        onWarnings: (w) => setWarnings(w),
      })) as NetworkPlacement;
      onAttached?.(created);
      onClose();
    } catch (err) {
      setSubmitError(err);
    } finally {
      setSubmitting(false);
    }
  };

  if (!isOpen) return null;

  return (
    <Modal variant={ModalVariant.medium} isOpen={isOpen} onClose={onClose} aria-labelledby="attach-network-title">
      <ModalHeader title={t('attach.title')} labelId="attach-network-title" description={t('attach.description')} />
      <ModalBody>
        <Form id="attach-network-form" onSubmit={(e) => { e.preventDefault(); void create(); }}>
          <FormGroup label={t('kindsOne.HybridNetwork')} fieldId="attach-network" isRequired>
            {fixedNetwork ? (
              <TextInput id="attach-network" value={fixedNetwork} readOnlyVariant="default" />
            ) : (
              <FormSelect id="attach-network" value={networkName} onChange={(_e, v) => setNetworkName(v)} isRequired>
                <FormSelectOption value="" label={networks.loading ? t('common.loading') : t('attach.chooseNetwork')} isDisabled />
                {networkChoices.map((n) => (
                  <FormSelectOption
                    key={n.name}
                    value={n.name}
                    label={n.block ? `${n.name} (${t('attach.noNamespaceShort')})` : n.name}
                    isDisabled={!!n.block}
                  />
                ))}
              </FormSelect>
            )}
            {(fieldErrors.byField.network || (env && greyedNetworks.length > 0) || (fixedNetwork && pairBlock)) && (
              <FormHelperText>
                <HelperText>
                  {fieldErrors.byField.network ? (
                    <HelperTextItem variant="error">{fieldErrors.byField.network}</HelperTextItem>
                  ) : env && (greyedNetworks.length > 0 || pairBlock) ? (
                    <HelperTextItem variant="warning">
                      {fixedNetwork ? t('attach.notAttachable', { network: networkName, environment: envLabel }) : t('attach.greyedNetworks')}{' '}
                      {networksHref(env) && (
                        <Link to={networksHref(env) as string} onClick={onClose}>
                          {t('attach.openEnvironmentNetworks', { environment: env.name })}
                        </Link>
                      )}
                    </HelperTextItem>
                  ) : null}
                </HelperText>
              </FormHelperText>
            )}
          </FormGroup>

          <FormGroup label={t('attach.environment')} fieldId="attach-env" isRequired>
            {fixedEnv ? (
              <TextInput id="attach-env" value={`${kindTerm(t, fixedEnv.kind)} ${fixedEnv.name}`} readOnlyVariant="default" />
            ) : (
              <FormSelect id="attach-env" value={envKey(env)} onChange={(_e, v) => setEnv(parseEnvKey(v))} isRequired>
                <FormSelectOption value="" label={t('attach.chooseEnvironment')} isDisabled />
                {envChoices.map((o) => (
                  <FormSelectOption
                    key={o.value}
                    value={o.value}
                    label={o.block ? `${o.label} (${t('attach.noNamespaceShort')})` : o.label}
                    isDisabled={!!o.block}
                  />
                ))}
              </FormSelect>
            )}
            <FormHelperText>
              <HelperText>
                {fieldErrors.byField.environment ? (
                  <HelperTextItem variant="error">{fieldErrors.byField.environment}</HelperTextItem>
                ) : env ? (
                  <HelperTextItem>{t(ENV_WHAT[env.kind])}</HelperTextItem>
                ) : null}
                {!fixedEnv && greyedEnvs.length > 0 && (
                  <HelperTextItem variant="warning">
                    {t('attach.greyedEnvironments')}{' '}
                    {greyedEnvs.map((o, i) => {
                      const href = networksHref({ kind: o.kind, name: o.name });
                      return (
                        <React.Fragment key={o.value}>
                          {i > 0 ? ', ' : ''}
                          {href ? (
                            <Link to={href} onClick={onClose}>
                              {t('attach.openEnvironmentNetworks', { environment: o.name })}
                            </Link>
                          ) : (
                            o.name
                          )}
                        </React.Fragment>
                      );
                    })}
                  </HelperTextItem>
                )}
              </HelperText>
            </FormHelperText>
          </FormGroup>

          {families.ipv4 && (
            <>
              <FormGroup label={t('attach.size')} fieldId="attach-size" isRequired>
                <FormSelect id="attach-size" value={prefixLength} onChange={(_e, v) => setPrefixLength(v)} isDisabled={!!pinnedText}>
                  {lengths.map((l) => (
                    <FormSelectOption key={l} value={String(l)} label={t('attach.sizeOption', { length: l, addresses: 2 ** (32 - l) })} />
                  ))}
                </FormSelect>
                <FormHelperText>
                  <HelperText>
                    {fieldErrors.byField.size ? <HelperTextItem variant="error">{fieldErrors.byField.size}</HelperTextItem> : null}
                    <HelperTextItem>
                      {env?.kind === 'CloudAWS'
                        ? t('attach.sizeLimitsAws', { min: PLACEMENT_IPV4_MIN, max: PLACEMENT_IPV4_MAX_AWS })
                        : t('attach.sizeLimits', { min: PLACEMENT_IPV4_MIN, max: PLACEMENT_IPV4_MAX })}
                      {space.length
                        ? ` ${space
                            .map((a) =>
                              a.largestFree != null
                                ? t('fields.placementSpaceFree', { prefix: a.prefix, free: a.largestFree })
                                : t('fields.placementSpaceFull', { prefix: a.prefix }),
                            )
                            .join(', ')}`
                        : ''}
                    </HelperTextItem>
                  </HelperText>
                </FormHelperText>
              </FormGroup>

              <FormGroup label={t('attach.pinned')} fieldId="attach-pinned">
                <TextInput
                  id="attach-pinned"
                  value={pinned}
                  onChange={(_e, v) => setPinned(v)}
                  placeholder={spacePrefixes[0] ?? ''}
                  validated={pinnedOk && !fieldErrors.byField.pinned ? 'default' : 'error'}
                  aria-describedby="attach-pinned-help"
                />
                <FormHelperText>
                  <HelperText id="attach-pinned-help">
                    {fieldErrors.byField.pinned ? <HelperTextItem variant="error">{fieldErrors.byField.pinned}</HelperTextItem> : null}
                    <HelperTextItem>{t('attach.pinnedHelp')}</HelperTextItem>
                  </HelperText>
                </FormHelperText>
              </FormGroup>
            </>
          )}

          <PlacementIpv6Fields
            idPrefix="attach"
            offer={v6Offer}
            networkHasIpv4={hasIpv4Space}
            value={v6}
            onChange={setV6}
            pinnedOk={v6PinnedOk}
          />
          {v6Note && <p className="sc-text-muted">{t(v6Note)}</p>}

          <ExpandableSection toggleText={t('form.advanced')}>
            <FormGroup label={t('attach.name')} fieldId="attach-name">
              <TextInput
                id="attach-name"
                value={nameTouched ? name : effectiveName}
                onChange={(_e, v) => {
                  setNameTouched(true);
                  setName(v);
                }}
                validated={!networkName || !env || nameOk ? 'default' : 'error'}
              />
              <FormHelperText>
                <HelperText>
                  {fieldErrors.byField.name ? <HelperTextItem variant="error">{fieldErrors.byField.name}</HelperTextItem> : null}
                  <HelperTextItem>{t('attach.nameHelp')}</HelperTextItem>
                </HelperText>
              </FormHelperText>
            </FormGroup>
          </ExpandableSection>
        </Form>

        {warnings.length > 0 && (
          <Alert variant="warning" isInline isPlain title={t('form.apiWarnings')} className="sc-mb">
            <ul>
              {warnings.map((w) => (
                <li key={w}>{w}</li>
              ))}
            </ul>
          </Alert>
        )}
        {errorTexts && (
          <Alert variant="danger" isInline title={errorTexts.title} className="sc-mb">
            {[errorTexts.body, ...causeLines(fieldErrors.other)].filter(Boolean).join(' ')}
          </Alert>
        )}
        {blockers.length > 0 && (
          <div className="sc-text-muted" aria-live="polite">
            {t('form.createBlockedBy')}
            <ul>
              {blockers.map((b) => (
                <li key={b}>{b}</li>
              ))}
            </ul>
          </div>
        )}
        {body && <p className="sc-text-muted">{t('durations.placement')}</p>}
      </ModalBody>
      <ModalFooter>
        <GatedButton
          kind="NetworkPlacement"
          verb="create"
          namespace={namespace}
          variant="primary"
          onClick={() => void create()}
          isLoading={submitting || checking}
          enabled={canCreate}
          disabledReason={checking ? t('form.checking') : blockers[0] ?? (shownError ? errorTexts?.title : t('form.checking'))}
        >
          {t('attach.create')}
        </GatedButton>
        <Button variant="link" onClick={onClose}>
          {t('common.cancel')}
        </Button>
      </ModalFooter>
    </Modal>
  );
}
