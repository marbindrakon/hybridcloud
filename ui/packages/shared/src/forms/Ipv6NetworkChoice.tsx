/**
 * The network form's IPv6 choice: from the platform (a block of the fabric's offered sizes, the
 * fabric's default preselected, optionally a specific block), the tenant's own prefixes (GUA or ULA),
 * or none. The choice maps onto the spec's fields (platformPrefixes / IPv6 entries in prefixes); there
 * is no mode field. No prefix value is ever prefilled.
 */
import React from 'react';
import { FormGroup, FormSelect, FormSelectOption, Radio, TextInput } from '@patternfly/react-core';
import { useTranslation } from '../i18n';
import { ListTextArea } from '../components/ListTextArea';
import type { Ipv6Mode, PlatformPrefixRequest } from '../types';
import {
  PLATFORM_BLOCK_LENGTHS,
  type AddressingError,
  type AddressingHint,
  type PlatformIpv6Offer,
} from './ipv6';

type T = (k: string, o?: Record<string, unknown>) => string;

export interface Ipv6ChoiceState {
  mode: Ipv6Mode;
  /** '' = the fabric's default size */
  blockLength: string;
  pinnedBlock: string;
  tenantPrefixes: string[];
}

/** The mode the form starts with: from the platform when the fabric reports platform IPv6 space, else none. */
export function initialIpv6Choice(offer: PlatformIpv6Offer | null | undefined): Ipv6ChoiceState {
  return { mode: offer ? 'Platform' : 'None', blockLength: '', pinnedBlock: '', tenantPrefixes: [] };
}

/** spec.platformPrefixes and the IPv6 entries of spec.prefixes for a choice. */
export function ipv6ChoiceSpec(c: Ipv6ChoiceState): { platformPrefixes: PlatformPrefixRequest[]; ipv6Prefixes: string[] } {
  if (c.mode === 'Tenant') return { platformPrefixes: [], ipv6Prefixes: c.tenantPrefixes };
  if (c.mode !== 'Platform') return { platformPrefixes: [], ipv6Prefixes: [] };
  const entry: PlatformPrefixRequest = { family: 6 };
  if (c.blockLength) entry.prefixLength = Number(c.blockLength);
  if (c.pinnedBlock.trim()) entry.prefix = c.pinnedBlock.trim();
  return { platformPrefixes: [entry], ipv6Prefixes: [] };
}

/** One error of the CRD mirror, in words. */
export function addressingErrorText(t: T, e: AddressingError): string {
  return t(`ipv6.err.${e.code}`, { prefix: e.prefix ?? '' });
}

/** One hint of a rule the platform checks after create, in words. */
export function addressingHintText(t: T, h: AddressingHint): string {
  return t(`ipv6.hint.${h.code}`, {
    prefix: h.prefix ?? '',
    network: h.network ?? '',
    lengths: (h.lengths ?? []).map((l) => `/${l}`).join(', '),
  });
}

export function Ipv6NetworkChoice({
  value,
  onChange,
  offer,
  errors,
  hints,
}: {
  value: Ipv6ChoiceState;
  onChange: (next: Ipv6ChoiceState) => void;
  /** The fabric's platform IPv6 offer; null = none; undefined = not known to the caller */
  offer: PlatformIpv6Offer | null | undefined;
  /** Errors of the IPv6 inputs (CRD mirror) */
  errors: AddressingError[];
  /** Rules the platform checks after create */
  hints: AddressingHint[];
}): React.ReactElement {
  const { t } = useTranslation();
  const set = (patch: Partial<Ipv6ChoiceState>) => onChange({ ...value, ...patch });
  const lengths = offer?.blockLengths.length ? offer.blockLengths : PLATFORM_BLOCK_LENGTHS;
  const pinnedErr = errors.find((e) => e.code.startsWith('platformPrefix') || e.code === 'platformMismatch');
  const tenantErrs = errors.filter((e) => ['notCanonical', 'length', 'scope', 'duplicate', 'overlap', 'tooMany'].includes(e.code));
  return (
    <FormGroup label={t('ipv6.choiceLabel')} fieldId="hn-ipv6" role="radiogroup" isStack>
      <Radio
        id="hn-ipv6-platform"
        name="hn-ipv6"
        label={t('ipv6.choicePlatform')}
        description={t('ipv6.choicePlatformHelp')}
        isChecked={value.mode === 'Platform'}
        onChange={() => set({ mode: 'Platform' })}
      />
      {value.mode === 'Platform' && (
        <div style={{ marginLeft: '1.5rem' }}>
          {offer === null && <p className="sc-text-muted">{t('ipv6.noPlatformSpace')}</p>}
          <FormGroup label={t('ipv6.blockSize')} fieldId="hn-ipv6-block">
            <FormSelect
              id="hn-ipv6-block"
              value={value.blockLength}
              onChange={(_e, v) => set({ blockLength: v })}
              aria-label={t('ipv6.blockSize')}
            >
              <FormSelectOption
                value=""
                label={offer?.defaultBlock ? t('ipv6.blockDefault', { length: offer.defaultBlock }) : t('ipv6.blockDefaultUnknown')}
              />
              {lengths.map((l) => (
                <FormSelectOption key={l} value={String(l)} label={`/${l}`} />
              ))}
            </FormSelect>
            <p className="sc-text-muted" style={{ marginTop: '0.25rem' }}>
              {offer?.blockLengths.length
                ? t('ipv6.blockHelpOffered', { lengths: offer.blockLengths.map((l) => `/${l}`).join(', ') })
                : t('ipv6.blockHelpUnknown')}
            </p>
          </FormGroup>
          <FormGroup label={t('ipv6.pinnedBlock')} fieldId="hn-ipv6-pinned">
            <TextInput
              id="hn-ipv6-pinned"
              value={value.pinnedBlock}
              onChange={(_e, v) => set({ pinnedBlock: v })}
              validated={value.pinnedBlock.trim() && pinnedErr ? 'error' : 'default'}
            />
            <p className="sc-text-muted" style={{ marginTop: '0.25rem' }}>
              {pinnedErr ? addressingErrorText(t, pinnedErr) : t('ipv6.pinnedBlockHelp')}
            </p>
          </FormGroup>
        </div>
      )}
      <Radio
        id="hn-ipv6-tenant"
        name="hn-ipv6"
        label={t('ipv6.choiceTenant')}
        description={t('ipv6.choiceTenantHelp')}
        isChecked={value.mode === 'Tenant'}
        onChange={() => set({ mode: 'Tenant' })}
      />
      {value.mode === 'Tenant' && (
        <div style={{ marginLeft: '1.5rem' }}>
          <FormGroup label={t('ipv6.tenantPrefixes')} fieldId="hn-ipv6-prefixes" isRequired>
            <ListTextArea
              id="hn-ipv6-prefixes"
              value={value.tenantPrefixes}
              onChange={(next) => set({ tenantPrefixes: next })}
              validated={value.tenantPrefixes.length && tenantErrs.length ? 'error' : 'default'}
              aria-label={t('ipv6.tenantPrefixes')}
            />
            <p className="sc-text-muted" style={{ marginTop: '0.25rem' }}>
              {value.tenantPrefixes.length && tenantErrs.length
                ? tenantErrs.map((e) => addressingErrorText(t, e)).join(' ')
                : t('ipv6.tenantPrefixesHelp')}
            </p>
          </FormGroup>
        </div>
      )}
      <Radio
        id="hn-ipv6-none"
        name="hn-ipv6"
        label={t('ipv6.choiceNone')}
        description={t('ipv6.choiceNoneHelp')}
        isChecked={value.mode === 'None'}
        onChange={() => set({ mode: 'None' })}
      />
      {hints.length > 0 && (
        <div className="sc-text-muted" aria-live="polite">
          {t('ipv6.hintsTitle')}
          <ul>
            {hints.map((h, i) => (
              <li key={`${h.code}-${i}`}>{addressingHintText(t, h)}</li>
            ))}
          </ul>
        </div>
      )}
    </FormGroup>
  );
}
