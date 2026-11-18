/**
 * A link the pages may open: http(s) only. A value read from an object's status is never trusted to be a URL
 * (javascript:, data: and other schemes are not rendered as links).
 */
export function safeLink(v: unknown): string | undefined {
  return typeof v === 'string' && /^https?:\/\//i.test(v.trim()) ? v.trim() : undefined;
}
