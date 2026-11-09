/** The Content-Security-Policy, built per request. Production has no 'unsafe-inline' and no 'unsafe-eval': scripts and styles
 *  run only with this request's nonce ('strict-dynamic' lets the nonced bootstrap load Next's chunks). Development adds
 *  'unsafe-eval' because React's dev tooling needs it; a test pins that production never does. */

export interface CspOptions {
  nonce: string;
  dev: boolean;
  apiOrigin: string; // where signed frame/export URLs and the WebSocket live
  wsOrigin: string;
  reportUri?: string;
  upgrade?: boolean; // upgrade-insecure-requests: only where the site itself is https (on http://localhost it would turn the API's http frame URLs into https)
}

export function buildCsp({ nonce, dev, apiOrigin, wsOrigin, reportUri, upgrade = !dev }: CspOptions): string {
  const directives: Record<string, string[]> = {
    "default-src": ["'self'"],
    "script-src": ["'self'", `'nonce-${nonce}'`, "'strict-dynamic'", ...(dev ? ["'unsafe-eval'"] : [])],
    "style-src": ["'self'", `'nonce-${nonce}'`],
    "img-src": ["'self'", "data:", apiOrigin],
    "font-src": ["'self'"],
    "connect-src": ["'self'", wsOrigin, ...(dev ? ["ws://localhost:*"] : [])],
    "media-src": ["'none'"],
    "object-src": ["'none'"],
    "frame-src": ["'none'"],
    "worker-src": ["'none'"],
    "manifest-src": ["'self'"],
    "base-uri": ["'self'"],
    "form-action": ["'self'"],
    "frame-ancestors": ["'none'"],
  };
  if (upgrade) directives["upgrade-insecure-requests"] = [];
  if (reportUri) directives["report-uri"] = [reportUri];
  return Object.entries(directives)
    .map(([name, values]) => [name, ...values].join(" "))
    .join("; ");
}

export function newNonce(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return btoa(String.fromCharCode(...bytes));
}
