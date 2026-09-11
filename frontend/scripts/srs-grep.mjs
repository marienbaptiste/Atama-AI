// The frontend half of the Golden Rule gate (spec §0, `npm run build` row): the browser never
// talks to WaniKani or Bunpro and never handles their tokens, so any of these in frontend/src
// fails the build. Runs as `prebuild` and `pretest`; there is no flag to skip it.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const SRC = fileURLToPath(new URL("../src/", import.meta.url));
const FORBIDDEN = [/wanikani\.com/i, /bunpro\.jp/i, /WANIKANI_TOKEN/, /BUNPRO_API_TOKEN/];

function* files(dir) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) yield* files(path);
    else yield path;
  }
}

const hits = [];
for (const path of files(SRC)) {
  readFileSync(path, "utf8").split(/\r?\n/).forEach((line, i) => {
    for (const rule of FORBIDDEN) {
      if (rule.test(line)) hits.push(`${relative(SRC, path)}:${i + 1}: ${rule} — ${line.trim()}`);
    }
  });
}

if (hits.length) {
  console.error("srs-grep: the page must never reach WaniKani/Bunpro or name their tokens (spec §0):");
  for (const hit of hits) console.error("  " + hit);
  process.exit(1);
}
console.log("srs-grep: OK");
