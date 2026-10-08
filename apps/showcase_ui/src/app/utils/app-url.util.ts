export function appUrl(path: string, baseURI = document.baseURI): string {
  const base = new URL('.', baseURI);
  const rawPath = path.split(/[?#]/)[0];
  if (/[\\\u0000-\u001f]/.test(path)) throw new Error('Invalid application URL');
  let decoded = rawPath;
  for (let pass = 0; pass < 3; pass++) {
    decoded = decodeURIComponent(decoded);
    if (decoded.split(/[\\/]/).includes('..') || decoded.includes('\\')) throw new Error('URL traversal is not allowed');
  }
  const target = new URL(path, base);
  if (!['http:', 'https:'].includes(target.protocol) || target.origin !== base.origin || target.username || target.password) {
    throw new Error('External application URL is not allowed');
  }
  const previewPath = target.pathname.match(/^\/preview\/pr\/[^/]+\//)?.[0];
  if (previewPath && previewPath !== base.pathname) throw new Error('Another preview is not allowed');
  if (!target.pathname.startsWith(base.pathname)) target.pathname = base.pathname + target.pathname.replace(/^\/+/, '');
  return target.pathname + target.search + target.hash;
}

export function mediaUrl(path: string | null | undefined, baseURI = document.baseURI): string | null {
  if (!path) return null;
  if (/^data:image\/(?:png|jpeg|webp|gif);base64,[a-z\d+/=\s]+$/i.test(path)) return path;
  try {
    if (path.startsWith('blob:') && new URL(path).origin === new URL(baseURI).origin) return path;
    return appUrl(path, baseURI);
  } catch {
    return null;
  }
}

export function storageKey(key: string, owner?: string | null, baseURI = document.baseURI): string | null {
  const prefix = new URL('.', baseURI).pathname;
  if (prefix === '/') return key;
  if (owner === undefined) return null;
  return `${key}:${prefix}:${owner === null ? 'open' : encodeURIComponent(owner)}`;
}

export function previewInfo(baseURI = document.baseURI): { number: string; sha: string } | null {
  const number = new URL('.', baseURI).pathname.match(/^\/preview\/pr\/([1-9]\d*)\/$/)?.[1];
  if (!number) return null;
  const sha = document.querySelector<HTMLMetaElement>('meta[name="artemis-preview-sha"]')?.content || '';
  return { number, sha: /^[a-f\d]{40}$/i.test(sha) ? sha.slice(0, 7) : 'unavailable' };
}
