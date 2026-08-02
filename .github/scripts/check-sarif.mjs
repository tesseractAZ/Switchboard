#!/usr/bin/env node
// Gate CI on CodeQL findings WITHOUT GitHub Advanced Security.
//
// The CodeQL workflow DOES upload to the Security tab (code scanning is free on
// public repositories). This script is the second, self-contained gate: it reads
// the same SARIF and fails the job in CI, so the build breaks on a finding rather
// than merely filing an alert someone has to notice.
// We print every result grouped by severity and FAIL the job on any
// genuinely actionable finding — an `error`-level result, or a rule whose
// `security-severity` is >= HIGH (7.0). Notes/warnings are surfaced but don't
// break the build (security-extended emits a fair number of low-signal notes).
//
// Usage: node check-sarif.mjs <path-to-sarif> [--fail-threshold <float>]
import { readFileSync } from 'node:fs';

// Reviewed + accepted findings (see .github/codeql-baseline.json). Matching is
// on BOTH rule id and exact path, so the same rule in a new file still fails.
// Absent/unreadable baseline = match nothing, i.e. fail closed.
let BASELINE = [];
try {
  BASELINE = JSON.parse(
    readFileSync(new URL('../codeql-baseline.json', import.meta.url), 'utf8'),
  ).accepted ?? [];
} catch {
  BASELINE = [];
}
const isBaselined = (ruleId, path) =>
  BASELINE.some((e) => e.rule === ruleId && (e.paths ?? []).includes(path));

const args = process.argv.slice(2);
const thrIdx = args.indexOf('--fail-threshold');
const FAIL_SEVERITY = thrIdx >= 0 ? Number(args[thrIdx + 1]) : 7.0; // HIGH
// Skip the flag AND its value when looking for the path — otherwise
// `--fail-threshold 8.0 report.sarif` took "8.0" as the file to read, so the
// script's own documented flag broke it whenever it came first.
const sarifPath = args.find(
  (a, i) => !a.startsWith('--') && !(thrIdx >= 0 && i === thrIdx + 1),
);

if (!sarifPath) {
  console.error('usage: check-sarif.mjs <sarif> [--fail-threshold <float>]');
  process.exit(2);
}

let sarif;
try {
  sarif = JSON.parse(readFileSync(sarifPath, 'utf8'));
} catch (e) {
  console.error(`Could not read/parse SARIF at ${sarifPath}: ${e.message}`);
  process.exit(2);
}

const runs = Array.isArray(sarif.runs) ? sarif.runs : [];
let total = 0;
const failing = [];
const byLevel = { error: 0, warning: 0, note: 0, none: 0 };

// A result CodeQL considers suppressed (an in-source `# codeql[rule-id]`
// comment) carries a non-empty `suppressions` array. Those are deliberate,
// reviewed decisions documented at the code site, so the gate must skip them —
// otherwise a justified suppression still breaks the build and the only way to
// stay green is to weaken the threshold for everything.
const isSuppressed = (r) => Array.isArray(r.suppressions) && r.suppressions.length > 0;
let suppressed = 0;

for (const run of runs) {
  // Build a ruleId -> {level, securitySeverity, name} index.
  //
  // ★ Rules live in tool.EXTENSIONS, not tool.driver. CodeQL emits an EMPTY
  //   `tool.driver.rules` and puts all 103 rule definitions — including the
  //   `security-severity` property this gate keys on — under
  //   `tool.extensions[*].rules`. Reading only the driver meant every lookup
  //   missed, every `sev` was NaN, and the `sev >= 7` half of the fail
  //   condition COULD NEVER FIRE. The gate silently degraded to "fail only on
  //   error-level results" while its own header advertised severity gating.
  //   Caught when enabling the Security-tab upload made GitHub report two
  //   findings as HIGH that this script had just called "no actionable
  //   findings" — the same value at 7.0 judged two different ways.
  //   Driver is still read first so a non-CodeQL SARIF keeps working.
  const ruleMeta = new Map();
  const ruleSources = [
    run?.tool?.driver?.rules ?? [],
    ...(run?.tool?.extensions ?? []).map((e) => e?.rules ?? []),
  ];
  for (const rules of ruleSources) {
    for (const r of rules) {
      ruleMeta.set(r.id, {
        level: r.defaultConfiguration?.level ?? 'warning',
        sev: Number(r.properties?.['security-severity'] ?? NaN),
        name: r.name ?? r.id,
      });
    }
  }
  if (ruleMeta.size === 0 && (run.results ?? []).length > 0) {
    // Results with no rule metadata anywhere: severity gating is impossible,
    // so say so rather than quietly passing on an unenforceable threshold.
    // FAIL. A gate that cannot evaluate its own threshold has not passed —
    // it has not run. Warning and exiting 0 reported a green check for an
    // unenforceable rule, which is exactly the false assurance this script
    // exists to prevent.
    console.error('ERROR: no rule metadata in driver OR extensions — severity gating is INERT for this SARIF.');
    console.error('Refusing to report a pass on a gate that cannot fire.');
    process.exit(2);
  }
  for (const res of run.results ?? []) {
    const resPath =
      res.locations?.[0]?.physicalLocation?.artifactLocation?.uri ?? '';
    if (isSuppressed(res) || isBaselined(res.ruleId, resPath)) {
      // Reviewed + justified (in-source, or in codeql-baseline.json). Counted
      // so the summary can never read "clean" when it means "all silenced".
      suppressed += 1;
      continue;
    }
    total += 1;
    const meta = ruleMeta.get(res.ruleId) ?? {};
    const level = res.level ?? meta.level ?? 'warning';
    const sev = Number.isFinite(meta.sev) ? meta.sev : NaN;
    byLevel[level] = (byLevel[level] ?? 0) + 1;
    const loc = res.locations?.[0]?.physicalLocation;
    const where = loc
      ? `${loc.artifactLocation?.uri ?? '?'}:${loc.region?.startLine ?? '?'}`
      : '(no location)';
    const msg = (res.message?.text ?? '').replace(/\s+/g, ' ').slice(0, 160);
    const isFail = level === 'error' || (Number.isFinite(sev) && sev >= FAIL_SEVERITY);
    const line = `  [${level}${Number.isFinite(sev) ? ` sev ${sev}` : ''}] ${res.ruleId} @ ${where}\n      ${msg}`;
    if (isFail) failing.push(line);
    console.log(line);
  }
}

console.log(
  `\nCodeQL results: ${total} total  (error:${byLevel.error} warning:${byLevel.warning} note:${byLevel.note})` +
    (suppressed ? `  [+${suppressed} accepted via baseline/suppression]` : '')
);

if (failing.length) {
  console.error(
    `\n✗ ${failing.length} actionable finding(s) — error-level or security-severity >= ${FAIL_SEVERITY}:`
  );
  for (const f of failing) console.error(f);
  process.exit(1);
}
console.log(`\n✓ No actionable findings (error-level or security-severity >= ${FAIL_SEVERITY}).`);
