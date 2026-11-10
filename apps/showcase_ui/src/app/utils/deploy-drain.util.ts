export function deployDrainMessage(error: { status?: number; error?: { detail?: unknown }; headers?: { get(name: string): string | null } }): string | null {
  const detail = error.error?.detail;
  if (error.status !== 503 || !detail || typeof detail !== 'object' || !('code' in detail) || detail.code !== 'server_draining') return null;
  const deadline = 'deadline' in detail ? Number(detail.deadline) : NaN;
  const remaining = Number.isFinite(deadline) && deadline > 0
    ? Math.max(1, Math.ceil(deadline - Date.now() / 1000))
    : Math.max(1, Number(error.headers?.get('Retry-After')) || 5);
  return `SmartQA đang cập nhật. Còn khoảng ${remaining} giây. Nội dung của bạn được giữ lại. Thử lại sau khi cập nhật.`;
}
