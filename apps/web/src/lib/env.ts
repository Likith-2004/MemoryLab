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
  const apiUrl = process.env.MEMORYLAB_API_URL ?? "http://127.0.0.1:8000";
  return {
    apiUrl: apiUrl.replace(/\/$/, ""),
    authUrl: (process.env.MEMORYLAB_AUTH_URL ?? "http://127.0.0.1:54321/auth/v1").replace(/\/$/, ""),
    publishableKey: required("MEMORYLAB_PUBLISHABLE_KEY"),
    publicApiUrl: (process.env.MEMORYLAB_PUBLIC_API_URL ?? apiUrl).replace(/\/$/, ""),
    siteUrl: (process.env.MEMORYLAB_SITE_URL ?? "http://localhost:3000").replace(/\/$/, ""),
  };
}

export function wsOrigin(publicApiUrl: string): string {
  return publicApiUrl.startsWith("https://") ? "wss://" + publicApiUrl.slice(8) : "ws://" + publicApiUrl.replace(/^http:\/\//, "");
}
