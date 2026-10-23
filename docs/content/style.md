# Content rules

These rules apply to the README, the guides under `docs/`, the samples, the CRD descriptions that
`oc explain` shows, and the strings of the dashboards and console plugins. `make check-content`
checks the parts that a script can check. The words themselves are in
[terms.yaml](terms.yaml).

## Voice

Write to the reader as "you". Start with the task, then explain. Use the display terms of
`terms.yaml` ("tenant", "access group", "network placement"), not the API kind names, except where
the reader types the kind (`oc get cloudvirts`) or reads it in YAML. On a detail page or in
`oc explain`, the kind name appears once, as the first words.

## Environments are always qualified

There are four environment types. Name the type every time: "OpenStack environment",
"OpenShift Virtualization environment" ("VM environment" in tables), "AWS environment",
"Google Cloud environment". In the same way, "project" is always qualified: an **app project** is
the SHC Project kind; an OpenStack project, a Google Cloud project and an OpenShift project are
other things.

## CRD descriptions

- The kind description starts with the display term and says what the object is, then ends its
  first sentence with "comparable to ..." and one equivalent per cloud from `terms.yaml`.
- A kind that is not implemented starts with "Not implemented."; a deprecated kind starts with
  "Deprecated."; an experimental kind starts with "Experimental.".
- Describe the API only: what a field means, its default, what happens when it changes. No plan
  or review identifiers, no dates, no people, no lab names, no internal variable names, no
  description of how the platform realizes the object beyond what the reader can observe.
- Fields the platform sets and the tenant should leave alone say so: "Set by the platform; leave
  unset."
- Status fields that are no longer written say "No longer set."

## Banned words

`terms.yaml` lists the words that are not used and what to write instead. Some apply everywhere
(retired component names, internal names); others only to tenant-facing text (for example
"Entity", "Rbac", "helper", "spoke", "CR"). Retired components are named only in the retired
components table.

## Examples and placeholders

Examples never use lab names. Use the tenant `example-corp` (namespace `entity-example-corp`),
the sites `site-vm` (OpenShift Virtualization), `site-osp` (OpenStack), `site-aws`, `site-gcp`,
`<tenant DNS domain>` and `<services apps domain>`, and documentation address ranges
(`198.51.100.0/24`, `2001:db8::/32`) for links; `10.0.0.0/8` and `fd00::/8` ranges for tenant
networks. Sample objects are named `example-*`, so they cannot collide with real objects.

## Adding a term or a kind

1. Add the kind under `kinds:` in `terms.yaml` (term, plural, audience, state, analogy).
2. Write its CRD description to the rules above.
3. Add its display names to the UI catalogues (`kindsOne`, `kinds`, and `kindsDense` when it has
   a dense term, in `ui/packages/shared/src/i18n/locales/en.json` and `fr.json`).
4. Run `make check-content`. It fails while a kind in the CRDs has no entry in `terms.yaml`, or
   the other way round, and while the English catalogue terms of a kind a tenant meets differ
   from `terms.yaml`.

## Labels built at runtime

Pages name kinds, object references and field keys through the vocabulary module
(`ui/packages/shared/src/i18n/vocabulary.ts`): `kindTerm` and `kindTermInText` for a kind,
`objectLabel` for a reference such as `CloudVirt/web` ("VM environment web"), `displayText` for
text from the API, and `fieldLabel` for a field key (`attributes.entity` reads "Attributes ·
Tenant"). `make check-content` fails on a raw kind passed into a display string, and on a banned
or `rendered` word (terms.yaml) in the labels the module builds for every kind a tenant meets and
every status field of those kinds (it needs `npm ci` in `ui/`; without it that part is skipped
with a warning). To check a crawl of rendered tenant pages, save each page's text and run
`python3 -I scripts/check-content.py --rendered <dir>`.
