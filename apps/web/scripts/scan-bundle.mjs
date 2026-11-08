// CI/gate step: fail if the built output contains secrets. Usage: node scripts/scan-bundle.mjs [dir]
import { existsSync } from "node:fs";
import { scanDir } from "./scan-lib.mjs";

const dir = process.argv[2] ?? ".next";
if (!existsSync(dir)) {
  console.error(`scan-bundle: ${dir} does not exist (run the build first)`);
  process.exit(2);
}
const forbidden = (process.env.MEMORYLAB_SCAN_FORBIDDEN ?? "").split(",").filter((v) => v.length >= 8);
const results = scanDir(dir, forbidden);
for (const r of results) console.error(`secret-like content [${r.rule}] in ${r.file}`);
if (results.length) process.exit(1);
console.log(`scan-bundle: ${dir} is clean`);
