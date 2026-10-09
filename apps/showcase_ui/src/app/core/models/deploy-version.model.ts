export interface DeployVersion {
  /** Full 40-character lowercase sha, or null when the server sent none. */
  sha: string | null;
  shortSha: string;
  deployedAt: string | null;
}

export const FULL_SHA = /^[0-9a-f]{40}$/i;

/** Parse `GET /api/system/version`; anything not a known version reads as null. */
export function parseDeployVersion(value: unknown): DeployVersion | null {
  if (typeof value !== 'object' || value === null) return null;
  const { status, sha, short_sha: shortSha, deployed_at: deployedAt } = value as Record<string, unknown>;
  if (status !== 'known' || typeof shortSha !== 'string' || !shortSha) return null;
  return {
    sha: typeof sha === 'string' && FULL_SHA.test(sha) ? sha.toLowerCase() : null,
    shortSha,
    deployedAt: typeof deployedAt === 'string' && !Number.isNaN(Date.parse(deployedAt)) ? deployedAt : null
  };
}
