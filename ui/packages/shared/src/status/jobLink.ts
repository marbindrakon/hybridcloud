/**
 * The admin's link to an automation job (pure): built from the job's id and the dashboard's setting
 * automationJobUrlTemplate (an https URL with "{jobId}", for example
 * https://automation.example.com/execution/jobs/playbook/{jobId}/output). Status carries no address: no
 * setting, no link.
 */
import { safeLink } from '../forms/safeLink';

export const JOB_ID_PLACEHOLDER = '{jobId}';

/** The link for a job, or undefined without a usable template or job id. */
export function automationJobUrl(template: string | undefined, jobId: unknown): string | undefined {
  const id = typeof jobId === 'number' ? String(jobId) : typeof jobId === 'string' ? jobId.trim() : '';
  if (!template || !id || !template.includes(JOB_ID_PLACEHOLDER)) return undefined;
  return safeLink(template.split(JOB_ID_PLACEHOLDER).join(encodeURIComponent(id)));
}
