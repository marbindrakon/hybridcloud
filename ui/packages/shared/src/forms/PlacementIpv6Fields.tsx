/**
 * The IPv6 part of the attach dialog and the placement form. It renders only where the environment's
 * site reports IPv6 and the network has IPv6 address space; otherwise one line says why there is no
 * IPv6 input (nothing for a network without IPv6). The source choice appears on public-cloud sites
 * only, with the sources the site offers; "the site's default" is preselected (the placement's status
 * reports the result).
 */
import React from 'react';
import { Checkbox, FormGroup, FormSelect, FormSelectOption, Radio, TextInput } from '@patternfly/react-core';
import { useTranslation } from '../i18n';
import type { PlacementIpv6Source } from '../types';
import type { PlacementFamilyBlock, PlacementIpv6Offer } from './ipv6';

export interface PlacementIpv6State {
  includeIpv4: boolean;
  includeIpv6: boolean;
  source: '' | PlacementIpv6Source;
  /** '' = the site's default size (public-cloud sites only) */
  prefixLength: string;
  pinned: string;
}

export const INITIAL_PLACEMENT_IPV6: PlacementIpv6State = {
  includeIpv4: true,
  includeIpv6: true,
  source: '',
  prefixLength: '',
  pinned: '',
};

/** The one-line note when the IPv6 section is hidden or the families cannot be met; null when none applies. */
export function placementIpv6NoteKey(offer: PlacementIpv6Offer, block: PlacementFamilyBlock | undefined): string | null {
  if (block === 'noFamilyHere') return 'attach.noFamilyHere';
  if (block === 'needIpv6Only') return 'attach.needIpv6Only';
  if (block === 'needFamily') return 'attach.needFamily';
  if (!offer.show && offer.block !== 'networkNoIpv6') return 'attach.ipv6NotOnSite';
  return null;
}

export function PlacementIpv6Fields({
  idPrefix,
  offer,
  networkHasIpv4,
  value,
  onChange,
  pinnedOk,
}: {
  idPrefix: string;
  offer: PlacementIpv6Offer;
  networkHasIpv4: boolean;
  value: PlacementIpv6State;
  onChange: (next: PlacementIpv6State) => void;
  pinnedOk: boolean;
}): React.ReactElement | null {
  const { t } = useTranslation();
  if (!offer.show) return null;
  const set = (patch: Partial<PlacementIpv6State>) => onChange({ ...value, ...patch });
  const provider = value.source === 'Provider';
  const lengths = offer.lengths;
  return (
    <FormGroup label={t('attach.ipv6Section')} fieldId={`${idPrefix}-ipv6`} isStack>
      {offer.ipv6Only && networkHasIpv4 && (
        <Checkbox
          id={`${idPrefix}-ipv4-on`}
          label={t('attach.includeIpv4')}
          isChecked={value.includeIpv4}
          onChange={(_e, v) => set({ includeIpv4: v })}
        />
      )}
      {networkHasIpv4 && (
        <Checkbox
          id={`${idPrefix}-ipv6-on`}
          label={t('attach.includeIpv6')}
          isChecked={value.includeIpv6}
          onChange={(_e, v) => set({ includeIpv6: v })}
        />
      )}
      {(value.includeIpv6 || !networkHasIpv4) && (
        <>
          {offer.sources.length > 0 && (
            <FormGroup label={t('attach.ipv6Source')} fieldId={`${idPrefix}-ipv6-source`} role="radiogroup" isStack>
              <Radio
                id={`${idPrefix}-ipv6-source-default`}
                name={`${idPrefix}-ipv6-source`}
                label={t('attach.sourceDefault')}
                isChecked={value.source === ''}
                onChange={() => set({ source: '' })}
              />
              {offer.sources.includes('Network') && (
                <Radio
                  id={`${idPrefix}-ipv6-source-network`}
                  name={`${idPrefix}-ipv6-source`}
                  label={t('attach.sourceNetwork')}
                  description={t('attach.sourceNetworkHelp')}
                  isChecked={value.source === 'Network'}
                  onChange={() => set({ source: 'Network' })}
                />
              )}
              {offer.sources.includes('Provider') && (
                <Radio
                  id={`${idPrefix}-ipv6-source-provider`}
                  name={`${idPrefix}-ipv6-source`}
                  label={t('attach.sourceProvider')}
                  description={t('attach.sourceProviderHelp')}
                  isChecked={provider}
                  onChange={() => set({ source: 'Provider', prefixLength: '', pinned: '' })}
                />
              )}
            </FormGroup>
          )}
          {!provider && (
            <>
              <FormGroup label={t('attach.ipv6Size')} fieldId={`${idPrefix}-ipv6-size`}>
                <FormSelect
                  id={`${idPrefix}-ipv6-size`}
                  value={value.prefixLength || (offer.sizeRequired ? String(lengths[0]) : '')}
                  onChange={(_e, v) => set({ prefixLength: v })}
                  isDisabled={!!value.pinned.trim()}
                  aria-label={t('attach.ipv6Size')}
                >
                  {!offer.sizeRequired && <FormSelectOption value="" label={t('attach.ipv6SizeDefault')} />}
                  {lengths.map((l) => (
                    <FormSelectOption key={l} value={String(l)} label={`/${l}`} />
                  ))}
                </FormSelect>
              </FormGroup>
              <FormGroup label={t('attach.ipv6Pinned')} fieldId={`${idPrefix}-ipv6-pinned`}>
                <TextInput
                  id={`${idPrefix}-ipv6-pinned`}
                  value={value.pinned}
                  onChange={(_e, v) => set({ pinned: v })}
                  validated={pinnedOk ? 'default' : 'error'}
                />
                <p className="sc-text-muted" style={{ marginTop: '0.25rem' }}>
                  {pinnedOk
                    ? t('attach.pinnedHelp')
                    : t('attach.ipv6PinnedInvalid', { lengths: lengths.map((l) => `/${l}`).join(', ') })}
                </p>
              </FormGroup>
            </>
          )}
        </>
      )}
    </FormGroup>
  );
}

/** The IPv6 size the form asks for: the chosen one, else the only size of a kind that needs one. */
export function effectiveIpv6Length(offer: PlacementIpv6Offer, value: PlacementIpv6State): string {
  return value.prefixLength || (offer.sizeRequired ? String(offer.lengths[0]) : '');
}
