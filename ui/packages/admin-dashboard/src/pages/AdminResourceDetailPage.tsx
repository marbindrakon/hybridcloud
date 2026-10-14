import React from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { Alert } from '@patternfly/react-core';
import { HybridSovereignKind, ResourceDetail } from '@hybridsovereign/shared';

interface AdminResourceDetailPageProps {
  kind: HybridSovereignKind;
  title: string;
  listPath: string;
  /** Fixed namespace when the URL has no :namespace segment (e.g. Entity) */
  fixedNamespace?: string;
  /** Tenant objects seen by the platform admin: no edit, reconcile or delete */
  readOnly?: boolean;
}

export function AdminResourceDetailPage({
  kind,
  title,
  listPath,
  fixedNamespace,
  readOnly,
}: AdminResourceDetailPageProps): React.ReactElement {
  const navigate = useNavigate();
  const { namespace: nsParam, name } = useParams<{ namespace?: string; name: string }>();
  const resourceName = name ? decodeURIComponent(name) : '';
  const namespace = fixedNamespace ?? (nsParam ? decodeURIComponent(nsParam) : '');

  if (!resourceName) {
    return (
      <Alert variant="warning" title="Missing resource name" isInline>
        No resource name in the URL.
      </Alert>
    );
  }

  if (!namespace) {
    return (
      <Alert variant="warning" title="Missing namespace" isInline>
        No namespace in the URL for this resource.
      </Alert>
    );
  }

  return (
    <ResourceDetail
      kind={kind}
      name={resourceName}
      namespace={namespace}
      parentTitle={title}
      parentPath={listPath}
      onBack={() => navigate(listPath)}
      onDeleted={() => navigate(listPath)}
      audience="admin"
      readOnly={readOnly}
    />
  );
}
