import { LoggerService, redact } from './logger.service';
import { appUrl } from '../utils/app-url.util';
import { DOCUMENT } from '@angular/common';
import { computed, inject, Injectable, InjectionToken, OnDestroy, signal } from '@angular/core';
import {
  AdbPacket,
  AdbPacketHeader,
  calculateChecksum
} from '@yume-chan/adb';
import type { AdbPacketData, AdbPacketInit } from '@yume-chan/adb';
import { PhoneTabService } from './phone-tab.service';
import {
  AdbDaemonWebUsbDevice,
  AdbDaemonWebUsbDeviceManager
} from '@yume-chan/adb-daemon-webusb';
import {
  Consumable,
  ReadableStream,
  StructDeserializeStream
} from '@yume-chan/stream-extra';
import type {
  ReadableStreamDefaultReader as TangoReadableStreamDefaultReader,
  WritableStreamDefaultWriter as TangoWritableStreamDefaultWriter
} from '@yume-chan/stream-extra';

/** `dropped` is a connection that was up and then lost; `error` is one that never came up. */
export type UsbDeviceRelayStatus = 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'dropped' | 'error';

export interface UsbDeviceRelayState {
  status: UsbDeviceRelayStatus;
  serial: string | null;
  /** The server's bridge session this tab holds; runs bind to it. Null unless connected. */
  sessionId: string | null;
  error: string | null;
}

export const WEBUSB_DEVICE_MANAGER = new InjectionToken<AdbDaemonWebUsbDeviceManager | undefined>(
  'WEBUSB_DEVICE_MANAGER',
  {
    providedIn: 'root',
    factory: () => AdbDaemonWebUsbDeviceManager.BROWSER
  }
);

export const DEVICE_BRIDGE_SOCKET_FACTORY = new InjectionToken<(url: string) => WebSocket>(
  'DEVICE_BRIDGE_SOCKET_FACTORY',
  {
    providedIn: 'root',
    factory: () => url => new WebSocket(url)
  }
);

const SOCKET_OPEN = 1;
const MAX_ADB_PAYLOAD_LENGTH = 1024 * 1024;
const SOCKET_OPEN_TIMEOUT_MS = 15_000;
const DEVICE_ATTACH_TIMEOUT_MS = 30_000;
const SOCKET_RECONNECT_GRACE_MS = 30_000;
const SOCKET_RECONNECT_INITIAL_DELAY_MS = 500;
const SOCKET_RECONNECT_MAX_DELAY_MS = 5_000;
const SOCKET_RECONNECT_OPEN_TIMEOUT_MS = 5_000;
type ClientCloseReason = 'manual_disconnect' | 'service_destroyed' | 'page_unload' |
  'connect_error' | 'socket_open_timeout' | 'attachment_timeout' | 'socket_error' |
  'socket_closed' | 'protocol_error' | 'bridge_rejected' | 'usb_read_error' |
  'usb_write_error' | 'usb_disconnected' | 'another_tab';

@Injectable({ providedIn: 'root' })
export class UsbDeviceRelayService implements OnDestroy {
  private readonly logger = inject(LoggerService);
  private readonly document = inject(DOCUMENT);
  private readonly deviceManager = inject(WEBUSB_DEVICE_MANAGER);
  private readonly socketFactory = inject(DEVICE_BRIDGE_SOCKET_FACTORY);
  private readonly browserWindow = this.document.defaultView;
  private readonly tabs = inject(PhoneTabService);

  public readonly isSupported = computed(() => this.deviceManager !== undefined);
  public readonly state = signal<UsbDeviceRelayState>({
    status: 'idle',
    serial: null,
    sessionId: null,
    error: null
  });

  /** The USB chooser is done and the bridge has not reported the phone yet. */
  public readonly attaching = signal(false);
  /** The phone another tab of this browser holds; null when none does. */
  public readonly heldInAnotherTab = this.tabs.heldElsewhere;

  private generation = 0;
  private socket: WebSocket | null = null;
  private device: AdbDaemonWebUsbDevice | null = null;
  private reader: TangoReadableStreamDefaultReader<AdbPacketData> | null = null;
  private writer: TangoWritableStreamDefaultWriter<Consumable<AdbPacketInit>> | null = null;
  private sessionLeased = false;
  private leasedSessionId: string | null = null;
  private socketOpenTimer: ReturnType<typeof setTimeout> | null = null;
  private attachmentTimer: ReturnType<typeof setTimeout> | null = null;
  private incomingPackets = Promise.resolve();
  private reconnectToken: string | null = null;
  private reconnectPromise: Promise<void> | null = null;
  private packetPumpGeneration: number | null = null;
  private socketWaiters: Array<() => void> = [];
  private reconnectWaiters: Array<(connected: boolean) => void> = [];
  private unloadWarningRegistered = false;

  private readonly warnBeforeUnload = (event: BeforeUnloadEvent): void => {
    const status = this.state().status;
    if (status === 'connecting' || status === 'connected' || status === 'reconnecting') {
      event.preventDefault();
      event.returnValue = '';
    }
  };

  private readonly closeBeforeUnload = (): void => {
    void this.finishConnection(this.generation, null, 'page_unload');
  };

  constructor() {
    // Another tab asked for the phone this tab holds: let go and say so.
    this.tabs.onReleaseRequest(() => {
      void this.finishConnection(this.generation, 'This phone is now used in another tab.', 'another_tab');
    });
  }

  public ngOnDestroy(): void {
    void this.finishConnection(this.generation, null, 'service_destroyed');
  }

  public async connect(): Promise<void> {
    if (['connecting', 'connected', 'reconnecting'].includes(this.state().status)) {
      return;
    }

    const generation = ++this.generation;
    this.attaching.set(false);
    this.state.set({ status: 'connecting', serial: null, sessionId: null, error: null });
    this.registerUnloadWarning();

    try {
      if (!this.deviceManager) {
        throw namedError('WebUsbUnsupportedError');
      }

      const device = await this.deviceManager.requestDevice();
      if (!device) {
        throw namedError('DeviceSelectionCancelledError');
      }
      if (generation !== this.generation) {
        await device.raw.close().catch((error: unknown) => this.logger.warn('Device close failed:', error));
        return;
      }

      this.attaching.set(true);
      this.device = device;
      const connection = await device.connect();
      if (generation !== this.generation) {
        await device.raw.close().catch((error: unknown) => this.logger.warn('Device close failed:', error));
        return;
      }

      this.reader = connection.readable.getReader();
      this.writer = connection.writable.getWriter();
      this.incomingPackets = Promise.resolve();

      const socket = this.socketFactory(this.createBridgeUrl());
      this.socket = socket;
      socket.binaryType = 'arraybuffer';
      await this.openSocket(socket, generation);
    } catch (error) {
      if (generation === this.generation) {
        // Closing the chooser is a choice, not a failure: back to no phone, with no error.
        await this.finishConnection(
          generation, isChooserCancel(error) ? null : this.toUserMessage(error), 'connect_error',
          this.device && !this.socket ? error : null
        );
      }
    }
  }

  public async disconnect(): Promise<void> {
    await this.finishConnection(this.generation, null, 'manual_disconnect');
  }

  private async openSocket(
    socket: WebSocket,
    generation: number,
    resume?: { sessionId: string; reconnectToken: string }
  ): Promise<void> {
    let failureReason: ClientCloseReason = 'connect_error';
    try {
      await new Promise<void>((resolve, reject) => {
      let opened = false;
      let settled = false;

      const fail = (error: Error, reason: ClientCloseReason): void => {
        if (generation !== this.generation) {
          return;
        }
        failureReason = reason;
        if (!settled) {
          settled = true;
          reject(error);
        } else {
          this.handleSocketFailure(socket, generation, reason, error);
        }
      };

      this.socketOpenTimer = setTimeout(() => {
        fail(namedError('DeviceBridgeConnectionError'), 'socket_open_timeout');
      }, resume ? SOCKET_RECONNECT_OPEN_TIMEOUT_MS : SOCKET_OPEN_TIMEOUT_MS);

      socket.onopen = () => {
        if (settled || generation !== this.generation) {
          return;
        }
        opened = true;
        settled = true;
        if (this.socketOpenTimer) {
          clearTimeout(this.socketOpenTimer);
          this.socketOpenTimer = null;
        }
        this.notifySocketWaiters();
        this.attachmentTimer = setTimeout(() => {
          void this.finishConnection(
            generation,
            'The bridge did not report an attached phone. Check the connection and try again.',
            'attachment_timeout'
          );
        }, DEVICE_ATTACH_TIMEOUT_MS);
        resolve();
        if (resume) {
          try {
            socket.send(JSON.stringify({
              type: 'resume', session_id: resume.sessionId,
              reconnect_token: resume.reconnectToken
            }));
          } catch (error) {
            this.handleSocketFailure(socket, generation, 'socket_error', error);
          }
        } else if (this.reader && this.packetPumpGeneration !== generation) {
          this.packetPumpGeneration = generation;
          void this.pumpDevicePackets(this.reader, generation);
        }
      };

      socket.onmessage = event => this.handleSocketMessage(event, generation);
      socket.onerror = () => fail(namedError('DeviceBridgeConnectionError'), 'socket_error');
      socket.onclose = event => {
        this.logger.warn('Device bridge socket closed:', {
          code: event.code, reason: event.reason, was_clean: event.wasClean,
          visibility_state: this.document.visibilityState
        });
        socket.onclose = null;
        fail(namedError(opened ? 'DeviceBridgeDroppedError' : 'DeviceBridgeConnectionError'), 'socket_closed');
      };
      });
    } catch (error) {
      if (resume) {
        throw error;
      }
      await this.finishConnection(generation, this.toUserMessage(error), failureReason);
    }
  }

  private async pumpDevicePackets(
    reader: TangoReadableStreamDefaultReader<AdbPacketData>,
    generation: number
  ): Promise<void> {
    let reason: ClientCloseReason = 'usb_read_error';
    try {
      while (generation === this.generation) {
        reason = 'usb_read_error';
        const { value, done } = await reader.read();
        if (done) {
          reason = 'usb_disconnected';
          throw namedError('UsbDeviceDisconnectedError');
        }
        reason = 'protocol_error';
        const serialized = serializeDevicePacket(value);
        const frame = new Uint8Array(serialized.byteLength);
        frame.set(serialized);
        while (generation === this.generation) {
          const socket = await this.waitForOpenSocket(generation);
          if (!socket) {
            return;
          }
          try {
            socket.send(frame.buffer);
            break;
          } catch (error) {
            reason = 'socket_error';
            this.handleSocketFailure(socket, generation, reason, error);
          }
        }
      }
    } catch (error) {
      if (generation === this.generation) {
        await this.finishConnection(
          generation, this.toUserMessage(error), reason, reason === 'usb_read_error' ? error : null
        );
      }
    }
  }

  private handleSocketFailure(
    socket: WebSocket,
    generation: number,
    reason: ClientCloseReason,
    error: unknown
  ): void {
    if (generation !== this.generation || this.socket !== socket) {
      return;
    }

    const current = this.state();
    if (
      (current.status === 'connected' || current.status === 'reconnecting') &&
      current.sessionId && this.reconnectToken && this.device && this.reader && this.writer &&
      (reason === 'socket_closed' || reason === 'socket_error')
    ) {
      this.logger.warn('Device bridge reconnecting:', {
        reason,
        error: errorDetails(error),
        session_id: current.sessionId,
        visibility_state: this.document.visibilityState
      });
      if (reason === 'socket_error' && socket.readyState === SOCKET_OPEN) {
        try {
          socket.send(JSON.stringify({
            type: 'client_close', reason, usb_error: null,
            visibility_state: this.document.visibilityState
          }));
        } catch {
          // The server detects the abnormal socket close if this report cannot be sent.
        }
      }
      socket.onopen = null;
      socket.onmessage = null;
      socket.onerror = null;
      socket.onclose = null;
      this.socket = null;
      if (socket.readyState < 2) {
        try {
          socket.close();
        } catch (closeError) {
          this.logger.warn('Device bridge socket close failed:', closeError);
        }
      }
      this.state.set({
        ...current,
        status: 'reconnecting',
        error: null
      });
      this.notifySocketWaiters();
      if (!this.reconnectPromise) {
        this.reconnectPromise = this.reconnectBridge(generation).finally(() => {
          this.reconnectPromise = null;
        });
      }
      return;
    }

    void this.finishConnection(generation, this.toUserMessage(error), reason);
  }

  private async reconnectBridge(generation: number): Promise<void> {
    const deadline = Date.now() + SOCKET_RECONNECT_GRACE_MS;
    let attempt = 0;

    while (generation === this.generation && Date.now() < deadline) {
      const delay = Math.min(
        SOCKET_RECONNECT_INITIAL_DELAY_MS * 2 ** Math.min(attempt, 4),
        SOCKET_RECONNECT_MAX_DELAY_MS,
        deadline - Date.now()
      );
      await new Promise<void>(resolve => setTimeout(resolve, delay));
      if (generation !== this.generation) {
        return;
      }

      const { sessionId } = this.state();
      const reconnectToken = this.reconnectToken;
      if (!sessionId || !reconnectToken) {
        break;
      }

      const socket = this.socketFactory(this.createBridgeUrl('/api/device-bridge/resume'));
      this.socket = socket;
      socket.binaryType = 'arraybuffer';
      try {
        await this.openSocket(socket, generation, { sessionId, reconnectToken });
        const timeout = Math.max(0, Math.min(
          SOCKET_RECONNECT_OPEN_TIMEOUT_MS,
          deadline - Date.now()
        ));
        if (await this.waitForReconnected(generation, timeout)) {
          return;
        }
      } catch {
        if (this.socket !== socket) {
          attempt += 1;
          continue;
        }
      }

      if (this.socket === socket) {
        this.clearSocketTimers();
        socket.onopen = null;
        socket.onmessage = null;
        socket.onerror = null;
        socket.onclose = null;
        this.socket = null;
        if (socket.readyState < 2) {
          try {
            socket.close();
          } catch (error) {
            this.logger.warn('Device bridge socket close failed:', error);
          }
        }
        this.notifySocketWaiters();
      }
      attempt += 1;
    }

    if (generation === this.generation && this.state().status === 'reconnecting') {
      await this.finishConnection(
        generation,
        'The device bridge connection dropped. Connect the phone again.',
        'socket_closed'
      );
    }
  }

  private async waitForOpenSocket(generation: number): Promise<WebSocket | null> {
    while (generation === this.generation) {
      const socket = this.socket;
      if (socket?.readyState === SOCKET_OPEN) {
        return socket;
      }
      await new Promise<void>(resolve => this.socketWaiters.push(resolve));
    }
    return null;
  }

  private waitForReconnected(generation: number, timeoutMs: number): Promise<boolean> {
    if (generation !== this.generation) {
      return Promise.resolve(false);
    }
    if (this.state().status === 'connected') {
      return Promise.resolve(true);
    }
    return new Promise(resolve => {
      const settle = (connected: boolean): void => {
        clearTimeout(timer);
        this.reconnectWaiters = this.reconnectWaiters.filter(waiter => waiter !== settle);
        resolve(connected);
      };
      const timer = setTimeout(() => settle(false), timeoutMs);
      this.reconnectWaiters.push(settle);
    });
  }

  private notifySocketWaiters(): void {
    const waiters = this.socketWaiters;
    this.socketWaiters = [];
    waiters.forEach(resolve => resolve());
  }

  private resolveReconnectWaiters(connected: boolean): void {
    const waiters = this.reconnectWaiters;
    this.reconnectWaiters = [];
    waiters.forEach(resolve => resolve(connected));
  }

  private clearSocketTimers(): void {
    if (this.socketOpenTimer) {
      clearTimeout(this.socketOpenTimer);
      this.socketOpenTimer = null;
    }
    if (this.attachmentTimer) {
      clearTimeout(this.attachmentTimer);
      this.attachmentTimer = null;
    }
  }

  private handleSocketMessage(event: MessageEvent, generation: number): void {
    if (generation !== this.generation) {
      return;
    }

    if (typeof event.data === 'string') {
      this.handleControlMessage(event.data, generation);
      return;
    }

    this.incomingPackets = this.incomingPackets.then(async () => {
      if (generation !== this.generation) {
        return;
      }
      const frame = await toUint8Array(event.data);
      if (generation !== this.generation) {
        return;
      }
      if (!this.sessionLeased) {
        throw namedError('DeviceBridgeProtocolError');
      }
      const packet = await deserializeBridgePacket(frame);
      if (generation !== this.generation || !this.writer) {
        return;
      }
      try {
        await this.writer.write(new Consumable(packet));
      } catch (error) {
        if (generation === this.generation) {
          await this.finishConnection(generation, this.toUserMessage(error), 'usb_write_error', error);
        }
      }
    }).catch((error) => {
      this.logger.error('Device bridge packet failed:', error);
      if (generation === this.generation) {
        void this.finishConnection(
          generation,
          'The device bridge sent invalid ADB data. Disconnect and try again.',
          'protocol_error'
        );
      }
    });
  }

  private handleControlMessage(message: string, generation: number): void {
    try {
      const value: unknown = JSON.parse(message);
      if (!isRecord(value) || typeof value.type !== 'string') {
        throw namedError('DeviceBridgeProtocolError');
      }

      if (value.type === 'session_leased') {
        if (
          typeof value.session_id !== 'string' ||
          value.session_id.length === 0 ||
          typeof value.expires_in_seconds !== 'number' ||
          !Number.isFinite(value.expires_in_seconds) ||
          value.expires_in_seconds <= 0 ||
          (value.reconnect_token !== undefined &&
            (typeof value.reconnect_token !== 'string' || value.reconnect_token.length > 128))
        ) {
          throw namedError('DeviceBridgeProtocolError');
        }
        const current = this.state();
        if (
          current.status === 'reconnecting' &&
          (value.session_id !== current.sessionId || value.reconnect_token !== this.reconnectToken)
        ) {
          throw namedError('DeviceBridgeProtocolError');
        }
        this.sessionLeased = true;
        this.leasedSessionId = value.session_id;
        if (typeof value.reconnect_token === 'string') {
          this.reconnectToken = value.reconnect_token;
        }
        return;
      }

      if (value.type === 'device_attached') {
        if (!this.sessionLeased || typeof value.serial !== 'string' || !value.serial.trim()) {
          throw namedError('DeviceBridgeProtocolError');
        }
        if (this.attachmentTimer) {
          clearTimeout(this.attachmentTimer);
          this.attachmentTimer = null;
        }
        this.attaching.set(false);
        this.state.set({
          status: 'connected',
          serial: value.serial.trim(),
          sessionId: this.leasedSessionId,
          error: null
        });
        this.tabs.announceHeld(value.serial.trim());
        this.resolveReconnectWaiters(true);
        return;
      }

      if (value.type === 'error') {
        throw namedError('DeviceBridgeRejectedError');
      }
    } catch (error) {
      if (generation === this.generation) {
        void this.finishConnection(
          generation, this.toUserMessage(error),
          error instanceof Error && error.name === 'DeviceBridgeRejectedError' ? 'bridge_rejected' : 'protocol_error'
        );
      }
    }
  }

  private async finishConnection(
    generation: number,
    error: string | null,
    reason: ClientCloseReason,
    usbError: unknown = null
  ): Promise<void> {
    if (generation !== this.generation ||
      (!this.socket && !this.device && ['idle', 'error'].includes(this.state().status))) {
      return;
    }

    const report = {
      type: 'client_close', reason,
      usb_error: usbError === null ? null : errorDetails(usbError),
      visibility_state: this.document.visibilityState
    };
    this.logger.warn('Device bridge closing:', report);
    this.generation += 1;
    const wasConnected = ['connected', 'reconnecting'].includes(this.state().status);
    this.state.set(error
      ? { status: wasConnected ? 'dropped' : 'error', serial: null, sessionId: null, error }
      : { status: 'idle', serial: null, sessionId: null, error: null });
    this.sessionLeased = false;
    this.leasedSessionId = null;
    this.reconnectToken = null;
    this.packetPumpGeneration = null;
    this.attaching.set(false);
    this.tabs.announceReleased();

    this.clearSocketTimers();
    this.notifySocketWaiters();
    this.resolveReconnectWaiters(false);

    const socket = this.socket;
    this.socket = null;
    if (socket) {
      socket.onopen = null;
      socket.onmessage = null;
      socket.onerror = null;
      socket.onclose = null;
      try {
        if (socket.readyState === SOCKET_OPEN) {
          socket.send(JSON.stringify(report));
        }
        if (socket.readyState < 2) {
          socket.close(1000, reason);
        }
      } catch (error) {
        this.logger.warn('Device bridge socket cleanup failed:', error);
        try {
          socket.close();
        } catch (error) {
          this.logger.warn('Device bridge socket close failed:', error);
        }
      }
    }

    const reader = this.reader;
    const writer = this.writer;
    const device = this.device;
    this.reader = null;
    this.writer = null;
    this.device = null;
    this.unregisterUnloadWarning();

    const cleanup: Promise<unknown>[] = [];
    if (reader) {
      cleanup.push(reader.cancel().catch(error => this.logger.warn('Device reader cleanup failed:', error)));
    }
    if (writer) {
      cleanup.push(writer.abort().catch(error => this.logger.warn('Device writer cleanup failed:', error)));
    }
    if (device && !reader && !writer) {
      cleanup.push(device.raw.close().catch((error: unknown) => this.logger.warn('Device close failed:', error)));
    }
    await Promise.allSettled(cleanup);
  }

  private registerUnloadWarning(): void {
    if (this.browserWindow && !this.unloadWarningRegistered) {
      this.browserWindow.addEventListener('beforeunload', this.warnBeforeUnload);
      this.browserWindow.addEventListener('pagehide', this.closeBeforeUnload);
      this.unloadWarningRegistered = true;
    }
  }

  private unregisterUnloadWarning(): void {
    if (this.browserWindow && this.unloadWarningRegistered) {
      this.browserWindow.removeEventListener('beforeunload', this.warnBeforeUnload);
      this.browserWindow.removeEventListener('pagehide', this.closeBeforeUnload);
      this.unloadWarningRegistered = false;
    }
  }

  private createBridgeUrl(path = '/api/device-bridge/session'): string {
    const location = this.document.location;
    if (!location) {
      throw namedError('DeviceBridgeConnectionError');
    }

    const url = new URL(appUrl(path, this.document.baseURI), location.href);
    if (url.protocol === 'https:') {
      url.protocol = 'wss:';
    } else if (
      url.protocol === 'http:' &&
      ['localhost', '127.0.0.1', '[::1]', '::1'].includes(url.hostname)
    ) {
      url.protocol = 'ws:';
    } else {
      throw namedError('WebUsbUnsupportedError');
    }
    return url.toString();
  }

  private toUserMessage(error: unknown): string {
    const errorObject = typeof error === 'object' && error !== null
      ? error as { name?: unknown; message?: unknown }
      : {};
    const name = typeof errorObject.name === 'string' ? errorObject.name : '';
    const message = typeof errorObject.message === 'string'
      ? errorObject.message
      : typeof error === 'string'
        ? error
        : '';
    if (
      error instanceof AdbDaemonWebUsbDevice.DeviceBusyError ||
      /claim(?:ing)? interface/i.test(message)
    ) {
      if (this.tabs.heldElsewhere()) {
        return 'This phone is connected in another tab. Use it there, or choose Use here.';
      }
      return 'Another program on this computer is using the phone (adb, Android Studio, scrcpy). ' +
        'Quit it or run adb kill-server, unplug and replug, then retry.';
    }

    switch (name) {
      case 'WebUsbUnsupportedError':
        return 'WebUSB is unavailable here. Use desktop Chromium over HTTPS or localhost.';
      case 'NotAllowedError':
        return 'USB permission was not granted. Allow access to the phone and try again.';
      case 'DeviceSelectionCancelledError':
      case 'NotFoundError':
        return 'No phone was selected. Choose an Android phone and try again.';
      case 'DeviceBridgeConnectionError':
        return 'Could not reach the device bridge. Check this page connection and try again.';
      case 'DeviceBridgeDroppedError':
        return 'The device bridge connection dropped. Connect the phone again.';
      case 'DeviceBridgeRejectedError':
        return 'The device bridge rejected the connection. Try again later.';
      case 'DeviceBridgeProtocolError':
        return 'The device bridge returned an invalid response. Disconnect and try again.';
      case 'UsbDeviceDisconnectedError':
        return 'The phone disconnected. Reconnect it to continue.';
      default:
        this.logger.error('Device bridge connection failed:', error);
        return 'Could not connect the phone. Check its cable and USB Debugging, then retry.\n' +
          `Details: ${name || 'Unknown error'}${message ? `: ${message}` : ''}`;
    }
  }
}

function errorDetails(error: unknown): string {
  try {
    if (typeof error === 'object' && error !== null && 'name' in error && 'message' in error) {
      return redact(`${error.name}: ${error.message}`).slice(0, 512);
    }
    return redact(String(error)).slice(0, 512);
  } catch {
    return 'Unknown USB error';
  }
}

/** The person closed the USB chooser without picking a phone. */
function isChooserCancel(error: unknown): boolean {
  const name = typeof error === 'object' && error !== null ? (error as { name?: unknown }).name : undefined;
  return name === 'DeviceSelectionCancelledError' || name === 'NotFoundError';
}

function namedError(name: string): Error {
  const error = new Error(name);
  error.name = name;
  return error;
}

interface BridgeControlMessage {
  type?: unknown;
  session_id?: unknown;
  expires_in_seconds?: unknown;
  reconnect_token?: unknown;
  serial?: unknown;
}

function isRecord(value: unknown): value is BridgeControlMessage {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

async function toUint8Array(data: unknown): Promise<Uint8Array> {
  if (data instanceof ArrayBuffer) {
    return new Uint8Array(data);
  }
  if (ArrayBuffer.isView(data)) {
    return new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
  }
  if (typeof Blob !== 'undefined' && data instanceof Blob) {
    return new Uint8Array(await data.arrayBuffer());
  }
  throw namedError('DeviceBridgeProtocolError');
}

function serializeDevicePacket(packet: AdbPacketData): Uint8Array {
  const rawPacket = packet as AdbPacketData & Partial<Pick<AdbPacket, 'checksum' | 'magic'>>;
  const payload = packet.payload;
  if (payload.byteLength > MAX_ADB_PAYLOAD_LENGTH) {
    throw namedError('DeviceBridgeProtocolError');
  }

  const command = packet.command >>> 0;
  const expectedMagic = (command ^ 0xffffffff) >>> 0;
  const magic = rawPacket.magic ?? expectedMagic;
  const checksum = rawPacket.checksum ?? 0;
  if (
    (magic >>> 0) !== expectedMagic ||
    (checksum !== 0 && checksum !== calculateChecksum(payload))
  ) {
    throw namedError('DeviceBridgeProtocolError');
  }

  return AdbPacket.serialize({
    command,
    arg0: packet.arg0,
    arg1: packet.arg1,
    checksum,
    magic,
    payload
  });
}

async function deserializeBridgePacket(frame: Uint8Array): Promise<AdbPacket> {
  const headerLength = AdbPacketHeader.size;
  if (frame.byteLength < headerLength || frame.byteLength > headerLength + MAX_ADB_PAYLOAD_LENGTH) {
    throw namedError('DeviceBridgeProtocolError');
  }

  const view = new DataView(frame.buffer, frame.byteOffset, frame.byteLength);
  const command = view.getUint32(0, true);
  const payloadLength = view.getUint32(12, true);
  const checksum = view.getUint32(16, true);
  const magic = view.getUint32(20, true);
  if (
    payloadLength !== frame.byteLength - headerLength ||
    magic !== ((command ^ 0xffffffff) >>> 0) ||
    (checksum !== 0 && checksum !== calculateChecksum(frame.subarray(headerLength)))
  ) {
    throw namedError('DeviceBridgeProtocolError');
  }

  const source = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(frame);
      controller.close();
    }
  });
  const reader = source.pipeThrough(new StructDeserializeStream(AdbPacket)).getReader();
  try {
    const packet = await reader.read();
    if (packet.done || !packet.value) {
      throw namedError('DeviceBridgeProtocolError');
    }
    const trailingPacket = await reader.read();
    if (!trailingPacket.done) {
      throw namedError('DeviceBridgeProtocolError');
    }
    return packet.value;
  } finally {
    reader.releaseLock();
  }
}
