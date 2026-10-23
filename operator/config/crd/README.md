# CRD sources

`bases/crd-<singular>.yaml` holds one CustomResourceDefinition per kind of the
`hybridsovereign.redhat` API group. These files are the only place a CRD is edited.

`operator/primary/helm/templates/crds.yaml` (what the primary operator chart installs) is
generated from them:

```
python3 -I scripts/gen-crds.py            # regenerate crds.yaml after editing a source
python3 -I scripts/gen-crds.py --check    # fail when crds.yaml is out of date (run by tests/run-tests.sh)
python3 -I scripts/gen-crds.py --compare <crds.yaml | dir of exported CRDs>   # data diff, e.g. against a cluster
make crds / make crds-check
```

Rules the generator enforces:

- One file per kind, named `crd-<spec.names.singular>.yaml`; any other file in `bases/` is an error.
- Exactly one `apiextensions.k8s.io/v1` CustomResourceDefinition per file, group `hybridsovereign.redhat`,
  `metadata.name` = `<plural>.hybridsovereign.redhat`, kind/plural/singular unique.
- Plain YAML: no Helm template text (`{{`).

A new kind is a new source file. Never edit or hand-merge `crds.yaml`: a merge conflict there is
resolved by regenerating it from the merged sources.
