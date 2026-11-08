import type { Metadata, Viewport } from "next";
import { headers } from "next/headers";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "Memory Lab", template: "%s · Memory Lab" },
  description: "Launch, watch, fork and compare game-playing agent runs that differ in one memory variable.",
};
export const viewport: Viewport = { width: "device-width", initialScale: 1 };

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  await headers(); // reading request headers makes every page dynamic: the CSP nonce is per request
  return (
    <html lang="en">
      <body>
        <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:p-2 focus:bg-panel">Skip to content</a>
        {children}
      </body>
    </html>
  );
}
