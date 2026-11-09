/** `next` must be a path on this site: no scheme, no host, no protocol-relative or backslash tricks. */
export function safeNext(value: string | null | undefined, fallback = "/runs"): string {
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.includes("\\") || /[\u0000-\u001f]/.test(value)) return fallback;
  return value;
}
