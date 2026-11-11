/**
 * The OpenShift cluster create form's network, publish, region, node count and address fields.
 * It reports what it would send (or the problems that stop it) through onChange; the parent
 * builds the spec with buildClusterSpec.
 */
import React, { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  ExpandableSection,
  FormGroup,
  FormHelperText,
  FormSelect,
  FormSelectOption,
  FormSelectOptionGroup,
  HelperText,
  HelperTextItem,
  TextInput,
} from '@patternfly/react-core';
import { useTranslation } from '../i18n';
import { useK8sResourceList } from '../hooks/k8s';
import { useTenantProfile } from '../hooks/tenantProfile';
import { useRouteFor } from '../nav/useNav';
import { AllowPlatformAccessButton } from '../components/AllowPlatformAccessButton';
import type { HybridNetwork, K8sResource, NetworkPlacement } from '../types';
import { environmentSiteName } from './ipv6';
import {
  COUNT_MAX,
  COUNT_MIN,
  EMPTY_NETWORKING,
  NETWORKING_LIMITS,
  OPENSHIFT_DEFAULTS,
  choiceValue,
  clusterFormProblems,
  defaultOverlapHints,
  machineNetworkApplies,
  parseChoice,
  type ClusterNetworkingInput,
  type ClusterProvider,
  type ClusterPublish,
  type CountProblem,
  type NetworkingField,
  type NetworkingProblem,
} from './clusterSpec';
import {
  PLATFORM_ACCESS_OFF_REASONS,
  defaultExternalChoice,
  emptyChoiceBlock,
  externalNetworkOptions,
  hybridNetworkOptions,
  networkSpace,
  networkingProblemText,
  reasonText,
  type ChoiceBlock,
} from './clusterNetworkChoice';

/** What the section would send, and whether it can be sent. */
export interface ClusterFieldsValue {
  network: string;
  publish: ClusterPublish;
  controlPlaneCount: string;
  workerCount: string;
  networking: ClusterNetworkingInput;
  /** Problems that stop the create (shown in the form's "not ready" list) */
  blockers: string[];
}

export function ClusterCreateFields({
  namespace,
  provider,
  environment,
  envResource,
  onChange,
}: {
  namespace: string;
  provider: ClusterProvider;
  environment: string;
  /** The chosen environment object (its site and region) */
  envResource?: K8sResource;
  onChange: (value: ClusterFieldsValue) => void;
}): React.ReactElement {
  const { t } = useTranslation();
  const routeFor = useRouteFor();
  const enabled = !!namespace;
  const networks = useK8sResourceList<HybridNetwork>('HybridNetwork', { namespace, enabled });
  const placements = useK8sResourceList<NetworkPlacement>('NetworkPlacement', { namespace, enabled });
  const { profile } = useTenantProfile(namespace, { enabled });

  const [network, setNetwork] = useState('');
  const [networkTouched, setNetworkTouched] = useState(false);
  const [publish, setPublish] = useState<ClusterPublish>('External');
  const [cp, setCp] = useState('');
  const [workers, setWorkers] = useState('');
  const [networking, setNetworking] = useState<ClusterNetworkingInput>(EMPTY_NETWORKING);

  // Another provider or environment: the choices start again.
  useEffect(() => {
    setNetwork('');
    setNetworkTouched(false);
    setPublish('External');
    setCp('');
    setWorkers('');
    setNetworking(EMPTY_NETWORKING);
  }, [provider, environment]);

  const site = environmentSiteName(envResource);
  const hybrid = useMemo(
    () =>
      hybridNetworkOptions({
        provider,
        environment,
        publish,
        networks: networks.items,
        placements: placements.items,
        profile,
      }),
    [provider, environment, publish, networks.items, placements.items, profile],
  );
  const external = provider === 'openstack' ? externalNetworkOptions(profile, site) : null;
  const preselect = defaultExternalChoice(external);

  // OpenStack: the site's default external network is preselected until the user chooses.
  useEffect(() => {
    if (provider === 'openstack' && !networkTouched && preselect && !network) setNetwork(choiceValue({ kind: 'external', name: preselect }));
  }, [provider, networkTouched, preselect, network]);

  const choice = parseChoice(network);
  const chosenNetwork = choice.kind === 'network' ? networks.items.find((n) => n.metadata.name === choice.name) : undefined;
  const chosenOption = choice.kind === 'network' ? hybrid.find((o) => o.name === choice.name) : undefined;
  let choiceBlock: ChoiceBlock | null = null;
  if (choice.kind === 'network') {
    choiceBlock = chosenOption ? chosenOption.block : { reason: 'WaitingForPlacement' };
  } else if (choice.kind === 'external') {
    const ext = external?.find((o) => o.name === choice.name);
    choiceBlock = external && !ext ? { reason: 'ExternalNetworkNotListed', params: { name: choice.name } } : ext?.block ?? null;
  } else {
    choiceBlock = emptyChoiceBlock(provider, external);
  }

  const ctx = {
    provider,
    hasNetwork: choice.kind === 'network',
    networkSpace: networkSpace(chosenNetwork),
    reserved: profile?.status?.reservedRanges ?? [],
    site,
  };
  const problems = clusterFormProblems(
    { provider, environment, network, publish, controlPlaneCount: cp, workerCount: workers, networking },
    ctx,
  );
  const hints = defaultOverlapHints(networking, ctx);
  const byField = (field: NetworkingField): NetworkingProblem | undefined => problems.networking.find((p) => p.field === field);
  const hintFor = (field: NetworkingField): NetworkingProblem | undefined => hints.find((p) => p.field === field);

  const countText = (p: CountProblem | null, role: 'controlPlane' | 'worker'): string =>
    p === 'integer'
      ? t('clusterNet.count.integer')
      : p === 'min'
        ? t('clusterNet.count.min', { min: COUNT_MIN[role] })
        : p === 'max'
          ? t('clusterNet.count.max', { max: COUNT_MAX })
          : '';

  const blockers: string[] = [];
  if (choiceBlock) blockers.push(reasonText(t, choiceBlock));
  if (problems.publish) blockers.push(t('clusterNet.publishInternalNeedsNetwork'));
  if (problems.controlPlane || problems.worker) blockers.push(t('clusterNet.blockersCounts'));
  if (problems.networking.length) blockers.push(t('clusterNet.blockersNetworking'));

  const value: ClusterFieldsValue = { network, publish, controlPlaneCount: cp, workerCount: workers, networking, blockers };
  const key = JSON.stringify(value);
  useEffect(() => {
    onChange(value);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const usable = hybrid.filter((o) => !o.block);
  const unusable = hybrid.filter((o) => o.block);
  const placementHref = (name?: string) => (name ? routeFor('NetworkPlacement', name) : undefined);
  const networkObj = (name: string) => networks.items.find((n) => n.metadata.name === name);

  /** The way to fix a refusal, where the product offers one. */
  const fixFor = (name: string, block: ChoiceBlock, placement?: string): React.ReactNode => {
    if (PLATFORM_ACCESS_OFF_REASONS.includes(block.reason)) {
      const obj = networkObj(name);
      return obj ? <AllowPlatformAccessButton network={obj} variant="link" isInline onDone={() => networks.refresh()} /> : null;
    }
    if (block.reason === 'RouterAttachmentsOff') {
      const href = placementHref(placement);
      return href ? <Link to={href}>{t('clusterNet.openPlacement')}</Link> : null;
    }
    return null;
  };

  const region =
    provider === 'aws'
      ? (envResource?.status as { awsRegion?: string } | undefined)?.awsRegion
      : provider === 'gcp'
        ? (envResource?.status as { region?: string } | undefined)?.region
        : undefined;

  const rangeField = (field: Exclude<NetworkingField, 'hostPrefix'>, helpKey: string) => {
    const id = `plat-${field}`;
    const problem = byField(field);
    const hint = hintFor(field);
    const limits = NETWORKING_LIMITS[field];
    return (
      <FormGroup label={t(`clusterNet.${field}`)} fieldId={id}>
        <TextInput
          id={id}
          value={networking[field]}
          onChange={(_e, v) => setNetworking((prev) => ({ ...prev, [field]: v }))}
          placeholder={OPENSHIFT_DEFAULTS[field]}
          validated={problem ? 'error' : 'default'}
          aria-describedby={`${id}-help`}
        />
        <FormHelperText>
          <HelperText id={`${id}-help`}>
            {problem ? (
              <HelperTextItem variant="error">{networkingProblemText(t, problem)}</HelperTextItem>
            ) : hint ? (
              <HelperTextItem variant="warning">
                {t('clusterNet.problem.defaultHint', { text: networkingProblemText(t, hint) })}
              </HelperTextItem>
            ) : (
              <HelperTextItem>{t(helpKey, { min: limits.min, max: limits.max, example: OPENSHIFT_DEFAULTS[field] })}</HelperTextItem>
            )}
          </HelperText>
        </FormHelperText>
      </FormGroup>
    );
  };

  const hostProblem = byField('hostPrefix');
  const hasEnv = !!environment;

  return (
    <>
      <FormGroup label={t('clusterNet.network')} fieldId="plat-network" isRequired={provider === 'openstack'}>
        <FormSelect
          id="plat-network"
          value={network}
          onChange={(_e, v) => {
            setNetworkTouched(true);
            setNetwork(v);
          }}
          isDisabled={!hasEnv}
          validated={hasEnv && choiceBlock ? 'error' : 'default'}
          aria-describedby="plat-network-help"
        >
          {provider === 'openstack' ? (
            <FormSelectOption
              value=""
              label={external === null ? t('clusterNet.siteDefault') : t('clusterNet.chooseNetwork')}
              isDisabled={external !== null}
            />
          ) : (
            <FormSelectOption value="" label={t('clusterNet.noNetworkCloud')} />
          )}
          {hybrid.length > 0 && (
            <FormSelectOptionGroup label={t('clusterNet.groupNetworks')}>
              {[...usable, ...unusable].map((o) => (
                <FormSelectOption
                  key={`n-${o.name}`}
                  value={choiceValue({ kind: 'network', name: o.name })}
                  label={o.subnet4 ? `${o.name} (${o.subnet4})` : o.name}
                  isDisabled={!!o.block}
                />
              ))}
            </FormSelectOptionGroup>
          )}
          {external && external.length > 0 && (
            <FormSelectOptionGroup label={t('clusterNet.groupExternal')}>
              {external.map((o) => (
                <FormSelectOption
                  key={`x-${o.name}`}
                  value={choiceValue({ kind: 'external', name: o.name })}
                  label={o.default ? t('clusterNet.defaultSuffix', { name: o.name }) : o.name}
                  isDisabled={!!o.block}
                />
              ))}
            </FormSelectOptionGroup>
          )}
        </FormSelect>
        <FormHelperText>
          <HelperText id="plat-network-help">
            {hasEnv && choiceBlock ? <HelperTextItem variant="error">{reasonText(t, choiceBlock)}</HelperTextItem> : null}
            <HelperTextItem>
              {provider === 'openstack'
                ? `${t('clusterNet.networkHelpOpenstack')} ${choice.kind === 'network' ? t('clusterNet.publishedOnNetwork') : t('clusterNet.publishedExternal')}`
                : t('clusterNet.networkHelpCloud')}
            </HelperTextItem>
            {hasEnv && !networks.loading && !placements.loading && hybrid.length === 0 && (
              <HelperTextItem>{t('clusterNet.noneAttached')}</HelperTextItem>
            )}
            {unusable.length > 0 && (
              <HelperTextItem variant="warning">
                {t('clusterNet.unusable')}
                <ul>
                  {unusable.map((o) => (
                    <li key={o.name}>
                      {t('clusterNet.unusableItem', { name: o.name, reason: reasonText(t, o.block as ChoiceBlock) })}{' '}
                      {fixFor(o.name, o.block as ChoiceBlock, o.placement)}
                    </li>
                  ))}
                </ul>
              </HelperTextItem>
            )}
          </HelperText>
        </FormHelperText>
      </FormGroup>

      {provider !== 'openstack' && (
        <FormGroup label={t('clusterNet.publish')} fieldId="plat-publish">
          <FormSelect
            id="plat-publish"
            value={publish}
            onChange={(_e, v) => setPublish(v === 'Internal' ? 'Internal' : 'External')}
            validated={problems.publish ? 'error' : 'default'}
            aria-describedby="plat-publish-help"
          >
            <FormSelectOption value="External" label={t('clusterNet.publishExternal')} />
            <FormSelectOption value="Internal" label={t('clusterNet.publishInternal')} />
          </FormSelect>
          <FormHelperText>
            <HelperText id="plat-publish-help">
              {problems.publish ? (
                <HelperTextItem variant="error">{t('clusterNet.publishInternalNeedsNetwork')}</HelperTextItem>
              ) : (
                <HelperTextItem>{t('clusterNet.publishHelp')}</HelperTextItem>
              )}
            </HelperText>
          </FormHelperText>
        </FormGroup>
      )}

      {provider !== 'openstack' && hasEnv && (
        <FormGroup label={t('clusterNet.region')} fieldId="plat-region">
          <TextInput id="plat-region" value={region ?? ''} readOnlyVariant="default" aria-describedby="plat-region-help" />
          <FormHelperText>
            <HelperText id="plat-region-help">
              <HelperTextItem>{region ? t('clusterNet.regionHelp') : t('clusterNet.regionUnknown')}</HelperTextItem>
            </HelperText>
          </FormHelperText>
        </FormGroup>
      )}

      <FormGroup label={t('clusterNet.controlPlaneCount')} fieldId="cp-count">
        <TextInput
          id="cp-count"
          type="number"
          min={COUNT_MIN.controlPlane}
          max={COUNT_MAX}
          value={cp}
          placeholder={t('clusterNet.countDefault')}
          onChange={(_e, v) => setCp(v)}
          validated={problems.controlPlane ? 'error' : 'default'}
          aria-describedby="cp-count-help"
        />
        <FormHelperText>
          <HelperText id="cp-count-help">
            {problems.controlPlane ? (
              <HelperTextItem variant="error">{countText(problems.controlPlane, 'controlPlane')}</HelperTextItem>
            ) : (
              <HelperTextItem>{t('clusterNet.controlPlaneHelp')}</HelperTextItem>
            )}
          </HelperText>
        </FormHelperText>
      </FormGroup>
      <FormGroup label={t('clusterNet.workerCount')} fieldId="worker-count">
        <TextInput
          id="worker-count"
          type="number"
          min={COUNT_MIN.worker}
          max={COUNT_MAX}
          value={workers}
          placeholder={t('clusterNet.countDefault')}
          onChange={(_e, v) => setWorkers(v)}
          validated={problems.worker ? 'error' : 'default'}
          aria-describedby="worker-count-help"
        />
        <FormHelperText>
          <HelperText id="worker-count-help">
            {problems.worker ? (
              <HelperTextItem variant="error">{countText(problems.worker, 'worker')}</HelperTextItem>
            ) : (
              <HelperTextItem>{t('clusterNet.workerHelp')}</HelperTextItem>
            )}
          </HelperText>
        </FormHelperText>
      </FormGroup>

      <ExpandableSection toggleText={t('clusterNet.advanced')}>
        <p className="sc-text-muted" style={{ marginBottom: '0.5rem' }}>
          {t('clusterNet.advancedHelp')}
        </p>
        {rangeField('clusterNetwork', 'clusterNet.clusterNetworkHelp')}
        <FormGroup label={t('clusterNet.hostPrefix')} fieldId="plat-hostPrefix">
          <TextInput
            id="plat-hostPrefix"
            type="number"
            min={NETWORKING_LIMITS.hostPrefix.min}
            max={NETWORKING_LIMITS.hostPrefix.max}
            value={networking.hostPrefix}
            placeholder={String(OPENSHIFT_DEFAULTS.hostPrefix)}
            onChange={(_e, v) => setNetworking((prev) => ({ ...prev, hostPrefix: v }))}
            validated={hostProblem ? 'error' : 'default'}
            aria-describedby="plat-hostPrefix-help"
          />
          <FormHelperText>
            <HelperText id="plat-hostPrefix-help">
              {hostProblem ? (
                <HelperTextItem variant="error">{networkingProblemText(t, hostProblem)}</HelperTextItem>
              ) : (
                <HelperTextItem>
                  {t('clusterNet.hostPrefixHelp', {
                    min: NETWORKING_LIMITS.hostPrefix.min,
                    max: NETWORKING_LIMITS.hostPrefix.max,
                    example: OPENSHIFT_DEFAULTS.hostPrefix,
                  })}
                </HelperTextItem>
              )}
            </HelperText>
          </FormHelperText>
        </FormGroup>
        {rangeField('serviceNetwork', 'clusterNet.serviceNetworkHelp')}
        {machineNetworkApplies(provider, choice.kind === 'network') ? (
          rangeField('machineNetwork', 'clusterNet.machineNetworkHelp')
        ) : (
          <FormGroup label={t('clusterNet.machineNetwork')} fieldId="plat-machineNetwork">
            <TextInput id="plat-machineNetwork" value={chosenOption?.subnet4 ?? ''} readOnlyVariant="default" aria-describedby="plat-machineNetwork-help" />
            <FormHelperText>
              <HelperText id="plat-machineNetwork-help">
                <HelperTextItem>{t('clusterNet.machineNetworkFromPlacement')}</HelperTextItem>
              </HelperText>
            </FormHelperText>
          </FormGroup>
        )}
      </ExpandableSection>
    </>
  );
}
