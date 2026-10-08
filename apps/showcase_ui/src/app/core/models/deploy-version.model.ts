export interface DeployVersion {
  shortSha: string;
  deployedAt: string | null;
}

/** Parse `GET /api/system/version`; anything not a known version reads as null. */
export function parseDeployVersion(value: unknown): DeployVersion | null {
  if (typeof value !== 'object' || value === null) return null;
  const { status, short_sha: shortSha, deployed_at: deployedAt } = value as Record<string, unknown>;
  if (status !== 'known' || typeof shortSha !== 'string' || !shortSha) return null;
  return {
    shortSha,
    deployedAt: typeof deployedAt === 'string' && !Number.isNaN(Date.parse(deployedAt)) ? deployedAt : null
  };
}
