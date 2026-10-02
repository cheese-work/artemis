import { DOCUMENT } from '@angular/common';
import { computed, inject, Injectable, InjectionToken, OnDestroy, signal } from '@angular/core';
import {
  AdbPacket,
  AdbPacketHeader,
  calculateChecksum
} from '@yume-chan/adb';
import type { AdbPacketData, AdbPacketInit } from '@yume-chan/adb';
import { AdbDaemonWebUsbDeviceManager } from '@yume-chan/adb-daemon-webusb';
import type { AdbDaemonWebUsbDevice } from '@yume-chan/adb-daemon-webusb';
import {
  Consumable,
  ReadableStream,
  StructDeserializeStream
} from '@yume-chan/stream-extra';
import type {
  ReadableStreamDefaultReader as TangoReadableStreamDefaultReader,
  WritableStreamDefaultWriter as TangoWritableStreamDefaultWriter
} from '@yume-chan/stream-extra';

export type UsbDeviceRelayStatus = 'idle' | 'connecting' | 'connected' | 'error';

export interface UsbDeviceRelayState {
  status: UsbDeviceRelayStatus;
  serial: string | null;
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

@Injectable({ providedIn: 'root' })
export class UsbDeviceRelayService implements OnDestroy {
  private readonly document = inject(DOCUMENT);
  private readonly deviceManager = inject(WEBUSB_DEVICE_MANAGER);
  private readonly socketFactory = inject(DEVICE_BRIDGE_SOCKET_FACTORY);
  private readonly browserWindow = this.document.defaultView;

  public readonly isSupported = computed(() => this.deviceManager !== undefined);
  public readonly state = signal<UsbDeviceRelayState>({
    status: 'idle',
    serial: null,
    error: null
  });

  private generation = 0;
  private socket: WebSocket | null = null;
  private device: AdbDaemonWebUsbDevice | null = null;
  private reader: TangoReadableStreamDefaultReader<AdbPacketData> | null = null;
  private writer: TangoWritableStreamDefaultWriter<Consumable<AdbPacketInit>> | null = null;
  private sessionLeased = false;
  private socketOpenTimer: ReturnType<typeof setTimeout> | null = null;
  private attachmentTimer: ReturnType<typeof setTimeout> | null = null;
  private incomingPackets = Promise.resolve();
  private unloadWarningRegistered = false;

  private readonly warnBeforeUnload = (event: BeforeUnloadEvent): void => {
    const status = this.state().status;
    if (status === 'connecting' || status === 'connected') {
      event.preventDefault();
      event.returnValue = '';
    }
  };

  public ngOnDestroy(): void {
    void this.disconnect();
  }

  public async connect(): Promise<void> {
    if (this.state().status === 'connecting' || this.state().status === 'connected') {
      return;
    }

    const generation = ++this.generation;
    this.state.set({ status: 'connecting', serial: null, error: null });
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
        await device.raw.close().catch(() => undefined);
        return;
      }

      this.device = device;
      const connection = await device.connect();
      if (generation !== this.generation) {
        await device.raw.close().catch(() => undefined);
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
        await this.finishConnection(generation, this.toUserMessage(error));
      }
    }
  }

  public async disconnect(): Promise<void> {
    await this.finishConnection(this.generation, null);
  }

  private async openSocket(socket: WebSocket, generation: number): Promise<void> {
    await new Promise<void>((resolve, reject) => {
      let opened = false;
      let settled = false;

      const fail = (error: Error): void => {
        if (generation !== this.generation) {
          return;
        }
        if (!settled) {
          settled = true;
          reject(error);
        } else {
          void this.finishConnection(generation, this.toUserMessage(error));
        }
      };

      this.socketOpenTimer = setTimeout(() => {
        fail(namedError('DeviceBridgeConnectionError'));
      }, SOCKET_OPEN_TIMEOUT_MS);

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
        this.attachmentTimer = setTimeout(() => {
          void this.finishConnection(
            generation,
            'The bridge did not report an attached phone. Check the connection and try again.'
          );
        }, DEVICE_ATTACH_TIMEOUT_MS);
        resolve();
        if (this.reader) {
          void this.pumpDevicePackets(this.reader, socket, generation);
        }
      };

      socket.onmessage = event => this.handleSocketMessage(event, generation);
      socket.onerror = () => fail(namedError('DeviceBridgeConnectionError'));
      socket.onclose = () => {
        fail(namedError(opened ? 'DeviceBridgeDroppedError' : 'DeviceBridgeConnectionError'));
      };
    });
  }

  private async pumpDevicePackets(
    reader: TangoReadableStreamDefaultReader<AdbPacketData>,
    socket: WebSocket,
    generation: number
  ): Promise<void> {
    try {
      while (generation === this.generation) {
        const { value, done } = await reader.read();
        if (done) {
          throw namedError('UsbDeviceDisconnectedError');
        }
        if (socket.readyState !== SOCKET_OPEN) {
          throw namedError('DeviceBridgeDroppedError');
        }
        const serialized = serializeDevicePacket(value);
        const frame = new Uint8Array(serialized.byteLength);
        frame.set(serialized);
        socket.send(frame.buffer);
      }
    } catch (error) {
      if (generation === this.generation) {
        await this.finishConnection(generation, this.toUserMessage(error));
      }
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
      await this.writer.write(new Consumable(packet));
    }).catch(() => {
      if (generation === this.generation) {
        void this.finishConnection(
          generation,
          'The device bridge sent invalid ADB data. Disconnect and try again.'
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
          value.expires_in_seconds <= 0
        ) {
          throw namedError('DeviceBridgeProtocolError');
        }
        this.sessionLeased = true;
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
        this.state.set({ status: 'connected', serial: value.serial.trim(), error: null });
        return;
      }

      if (value.type === 'error') {
        throw namedError('DeviceBridgeRejectedError');
      }
    } catch (error) {
      if (generation === this.generation) {
        void this.finishConnection(generation, this.toUserMessage(error));
      }
    }
  }

  private async finishConnection(generation: number, error: string | null): Promise<void> {
    if (generation !== this.generation) {
      return;
    }

    this.generation += 1;
    this.state.set(error
      ? { status: 'error', serial: null, error }
      : { status: 'idle', serial: null, error: null });
    this.sessionLeased = false;

    if (this.socketOpenTimer) {
      clearTimeout(this.socketOpenTimer);
      this.socketOpenTimer = null;
    }
    if (this.attachmentTimer) {
      clearTimeout(this.attachmentTimer);
      this.attachmentTimer = null;
    }

    const socket = this.socket;
    this.socket = null;
    if (socket) {
      socket.onopen = null;
      socket.onmessage = null;
      socket.onerror = null;
      socket.onclose = null;
      try {
        if (socket.readyState === SOCKET_OPEN) {
          socket.send('close');
        }
        if (socket.readyState < 2) {
          socket.close(1000, 'Client disconnected');
        }
      } catch {
        try {
          socket.close();
        } catch {}
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
      cleanup.push(reader.cancel().catch(() => undefined));
    }
    if (writer) {
      cleanup.push(writer.abort().catch(() => undefined));
    }
    if (device && !reader && !writer) {
      cleanup.push(device.raw.close().catch(() => undefined));
    }
    await Promise.allSettled(cleanup);
  }

  private registerUnloadWarning(): void {
    if (this.browserWindow && !this.unloadWarningRegistered) {
      this.browserWindow.addEventListener('beforeunload', this.warnBeforeUnload);
      this.unloadWarningRegistered = true;
    }
  }

  private unregisterUnloadWarning(): void {
    if (this.browserWindow && this.unloadWarningRegistered) {
      this.browserWindow.removeEventListener('beforeunload', this.warnBeforeUnload);
      this.unloadWarningRegistered = false;
    }
  }

  private createBridgeUrl(): string {
    const location = this.document.location;
    if (!location) {
      throw namedError('DeviceBridgeConnectionError');
    }

    const url = new URL('/api/device-bridge/session', location.href);
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
    const name = error instanceof Error ? error.name : '';
    switch (name) {
      case 'WebUsbUnsupportedError':
        return 'WebUSB is unavailable here. Use desktop Chromium over HTTPS or localhost.';
      case 'NotAllowedError':
        return 'USB permission was not granted. Allow access to the phone and try again.';
      case 'DeviceSelectionCancelledError':
      case 'NotFoundError':
        return 'No phone was selected. Choose an Android phone and try again.';
      case 'DeviceBusyError':
        return 'The phone is busy. Close other ADB tools using its USB connection, then retry.';
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
        return 'Could not connect the phone. Check its cable and USB Debugging, then retry.';
    }
  }
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
