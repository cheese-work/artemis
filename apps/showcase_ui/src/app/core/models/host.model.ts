import { DeviceKind } from './system.model';

export type ComputerStatus = 'online' | 'reconnecting' | 'offline' | 'update_required' | 'revoked';

export interface Computer {
  id: string;
  name: string;
  os: string | null;
  agent_version: string | null;
  protocol_version: number | null;
  status: ComputerStatus;
  reason: string | null;
  /** Epoch seconds when the status last changed. */
  since: number;
  phones_shared: number;
  phones_not_shared: number;
  unshared_serials: string[];
  share_command: string;
  active_run_count: number;
}

export interface RegistryDevice {
  serial: string;
  model: string | null;
  /** Classified from adb properties; absent when the computer's agent did not report it. */
  device_kind?: DeviceKind;
  source: 'browser' | 'computer';
  /** Verified email of the person who connected a browser phone; null for a computer's phone. */
  owner?: string | null;
  computer_id: string | null;
  computer_name: string | null;
  computer_status: ComputerStatus;
  reason: string | null;
  since: number | null;
}

export interface HostsResponse {
  enabled: boolean;
  hosts: Computer[];
  devices: RegistryDevice[];
}

export interface EnrollmentCode {
  code_id: string;
  code: string;
  /** Epoch seconds. */
  expires_at: number;
}

export interface EnrollmentCodeStatus {
  /** `enrolled` = the computer registered but has not yet authenticated a connection. */
  status: 'waiting' | 'enrolled' | 'connected' | 'expired';
  computer_name: string | null;
}
