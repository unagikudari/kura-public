// AUTO-RUN by `npm run prebuild`. Writes src/lib/build_info.ts with
// VERSION + git commit/branch + build timestamp so the web header can
// display exactly which build is deployed.
const { execSync } = require('child_process');
const fs = require('fs');
const path = require('path');

function safe(cmd, fallback = 'unknown') {
  try {
    return execSync(cmd, { encoding: 'utf8', stdio: ['pipe', 'pipe', 'ignore'] }).trim();
  } catch {
    return fallback;
  }
}

const repoRoot = path.join(__dirname, '..', '..');
const versionFile = path.join(repoRoot, 'VERSION');
const version = fs.existsSync(versionFile)
  ? fs.readFileSync(versionFile, 'utf8').trim()
  : '0.0.0';

// Read last release date from CHANGELOG.md. Matches both legacy
// `## [x.y.z] - YYYY-MM-DD` and current `## x.y.z — YYYY-MM-DD` (em dash).
let releaseDate = 'unknown';
const changelog = path.join(repoRoot, 'CHANGELOG.md');
if (fs.existsSync(changelog)) {
  const txt = fs.readFileSync(changelog, 'utf8');
  const m = txt.match(/^##\s*\[?(\d+\.\d+\.\d+)\]?\s*[-—]\s*(\d{4}-\d{2}-\d{2})/m);
  if (m) releaseDate = m[2];
}

const info = {
  version,
  releaseDate,
  commit: safe('git rev-parse --short HEAD'),
  branch: safe('git rev-parse --abbrev-ref HEAD'),
  dirty: safe('git status --porcelain --untracked-files=no').length > 0,
  buildTime: new Date().toISOString(),
};

const out = `// AUTO-GENERATED at build time by web/scripts/gen-build-info.cjs.
// Do not edit by hand — changes will be overwritten.
export const BUILD_INFO = ${JSON.stringify(info, null, 2)} as const;
`;

const outPath = path.join(__dirname, '..', 'src', 'lib', 'build_info.ts');
fs.writeFileSync(outPath, out);
console.log('[gen-build-info]', info);
