export interface StorageWarning {
  level: 'warning' | 'critical';
  code: 'low_disk' | 'insufficient_storage';
  message: string;
}

export interface RetentionSettings {
  enabled: boolean;
  days: number;
}

export interface StorageReport {
  usage_bytes: number;
  run_count: number;
  clearable_count: number;
  pinned_count: number;
  pinned_bytes: number;
  disk: { total_bytes: number; free_bytes: number; free_percent: number };
  warnings: StorageWarning[];
  retention: RetentionSettings;
}
