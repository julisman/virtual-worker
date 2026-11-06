// Tiny semver comparator for template version badges (PRD-29 M1) — no
// dependency. Handles numeric major.minor.patch plus an optional
// dot-separated prerelease (a prerelease sorts before its release, per
// semver 2.0 precedence rules §11); anything unparsable falls back to 0.0.0.

function parse(v: string): { core: [number, number, number]; pre: string[] } {
  const [core, ...preParts] = String(v || "0.0.0").split("-");
  const pre = preParts.join("-").split(".").filter(Boolean);
  const nums = core.split(".").map((n) => parseInt(n, 10) || 0);
  return { core: [nums[0] || 0, nums[1] || 0, nums[2] || 0], pre };
}

/** Compare two semver-ish strings: negative if a < b, 0 if equal, positive if a > b. */
export function compareSemver(a: string, b: string): number {
  const pa = parse(a);
  const pb = parse(b);
  for (let i = 0; i < 3; i++) {
    if (pa.core[i] !== pb.core[i]) return pa.core[i] - pb.core[i];
  }
  if (pa.pre.length === 0 && pb.pre.length === 0) return 0;
  if (pa.pre.length === 0) return 1;   // no prerelease outranks any prerelease
  if (pb.pre.length === 0) return -1;
  const len = Math.max(pa.pre.length, pb.pre.length);
  for (let i = 0; i < len; i++) {
    const x = pa.pre[i];
    const y = pb.pre[i];
    if (x === undefined) return -1;
    if (y === undefined) return 1;
    if (x === y) continue;
    const xNum = /^\d+$/.test(x);
    const yNum = /^\d+$/.test(y);
    if (xNum && yNum) return parseInt(x, 10) - parseInt(y, 10);
    if (xNum !== yNum) return xNum ? -1 : 1;
    return x < y ? -1 : 1;
  }
  return 0;
}
