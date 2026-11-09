import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";
import local from "./eslint-rules/plugin.mjs";

const config = [
  ...nextVitals,
  ...nextTs,
  { ignores: [".next/**", "node_modules/**", "next-env.d.ts", "playwright-report/**", "test-results/**"] },
  {
    files: ["src/**/*.{ts,tsx}", "e2e/**/*.ts"],
    plugins: { memorylab: local },
    rules: { "memorylab/no-raw-html": "error", "no-eval": "error", "no-implied-eval": "error", "no-new-func": "error" },
  },
];

export default config;
