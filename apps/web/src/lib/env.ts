/** Server-side configuration. Nothing here reaches the browser bundle except what a server component passes down on purpose. */

function required(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is not set`);
  return value;
}

export interface WebEnv {
  apiUrl: string; // the API, as the web server reaches it
  authUrl: string; // GoTrue, used ONLY for /token and /logout (never /signup, /otp or /recover)
  publishableKey: string;
  publicApiUrl: string; // the API as the browser reaches it (signed frame/export URLs, WebSocket)
  siteUrl: string;
}

export function webEnv(): WebEnv {
  const env = readEnv();
  if (process.env.NODE_ENV === "production" && process.env.NEXT_PHASE !== "phase-production-build") {
    const problems = productionProblems(env, internalHttpHosts());
    if (trustedHops() < 1) problems.push("MEMORYLAB_WEB_TRUSTED_HOPS must be at least 1 in production (without a trusted load balancer every visitor would share one client address at the API)");
    if (problems.length) throw new Error(`refusing to start: ${problems.join("; ")}`);
  }
  return env;
}

export function trustedHops(): number {
  const n = Number(process.env.MEMORYLAB_WEB_TRUSTED_HOPS ?? "0");
  return Number.isInteger(n) && n >= 0 && n <= 10 ? n : 0;
}

/** Exact hostnames (a compose service name, say) that this server may reach over plain http on a private network: MEMORYLAB_INTERNAL_HTTP_HOSTS=api,kong.
 *  Only apiUrl and authUrl may use them; the URLs a browser sees (publicApiUrl, siteUrl) never can. */
export function internalHttpHosts(): string[] {
  return (process.env.MEMORYLAB_INTERNAL_HTTP_HOSTS ?? "").split(",").map((h) => h.trim().toLowerCase()).filter(Boolean);
}

function readEnv(): WebEnv {
  const apiUrl = process.env.MEMORYLAB_API_URL ?? "http://127.0.0.1:8000";
  return {
    apiUrl: apiUrl.replace(/\/$/, ""),
    authUrl: (process.env.MEMORYLAB_AUTH_URL ?? "http://127.0.0.1:54321/auth/v1").replace(/\/$/, ""),
    publishableKey: required("MEMORYLAB_PUBLISHABLE_KEY"),
    publicApiUrl: (process.env.MEMORYLAB_PUBLIC_API_URL ?? apiUrl).replace(/\/$/, ""),
    siteUrl: (process.env.MEMORYLAB_SITE_URL ?? "http://localhost:3000").replace(/\/$/, ""),
  };
}

function isLoopback(host: string): boolean {
  return host === "localhost" || host.endsWith(".localhost") || host === "[::1]" || /^127\.\d+\.\d+\.\d+$/.test(host);
}

/** In production, nothing the browser or this server sends may go over plain http/ws to a remote host (keys and tokens ride on it).
 *  Returns the problems; webEnv() throws on any, so a misconfigured deployment fails at start, not at the first login. */
export function productionProblems(e: { apiUrl: string; authUrl: string; publicApiUrl: string; siteUrl: string }, internalHosts: string[] = []): string[] {
  const problems: string[] = [];
  for (const name of ["apiUrl", "authUrl", "publicApiUrl", "siteUrl"] as const) {
    const u = new URL(e[name]);
    const internal = (name === "apiUrl" || name === "authUrl") && u.protocol === "http:" && internalHosts.some((h) => h.toLowerCase() === u.hostname.toLowerCase());
    if (u.protocol !== "https:" && !isLoopback(u.hostname) && !internal) problems.push(`${name} must be https:// in production (got ${u.protocol}//${u.hostname})`);
  }
  return problems;
}

export function wsOrigin(publicApiUrl: string): string {
  return publicApiUrl.startsWith("https://") ? "wss://" + publicApiUrl.slice(8) : "ws://" + publicApiUrl.replace(/^http:\/\//, "");
}
