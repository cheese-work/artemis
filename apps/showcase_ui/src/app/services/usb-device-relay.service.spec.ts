import { TestBed } from '@angular/core/testing';
import {
  AdbCommand,
  AdbPacket,
  AdbPacketData,
  AdbPacketInit
} from '@yume-chan/adb';
import {
  AdbDaemonWebUsbDevice,
  AdbDaemonWebUsbDeviceManager
} from '@yume-chan/adb-daemon-webusb';
import { Consumable } from '@yume-chan/stream-extra';
import { LoggerService } from './logger.service';
import { PHONE_TAB_CHANNEL, TabChannel } from './phone-tab.service';
import {
  DEVICE_BRIDGE_SOCKET_FACTORY,
  UsbDeviceRelayService,
  WEBUSB_DEVICE_MANAGER
} from './usb-device-relay.service';

/** A tab channel that records what this tab says and lets a spec speak as another tab. */
class FakeTabChannel implements TabChannel {
  public posted: { type: string; serial?: string; tab?: string }[] = [];
  private listeners: ((event: MessageEvent) => void)[] = [];
  public postMessage(message: unknown): void { this.posted.push(message as { type: string }); }
  public addEventListener(_type: 'message', listener: (event: MessageEvent) => void): void { this.listeners.push(listener); }
  public removeEventListener(_type: 'message', listener: (event: MessageEvent) => void): void {
    this.listeners = this.listeners.filter((l) => l !== listener);
  }
  public close(): void { this.listeners = []; }
  public fromOtherTab(message: object): void {
    this.listeners.forEach((l) => l({ data: { tab: 'other', ...message } } as MessageEvent));
  }
}

class FakeSocket {
  public binaryType: BinaryType = 'blob';
  public readyState = 0;
  public onopen: ((event: Event) => void) | null = null;
  public onmessage: ((event: MessageEvent) => void) | null = null;
  public onerror: ((event: Event) => void) | null = null;
  public onclose: ((event: CloseEvent) => void) | null = null;
  public sent: Array<string | ArrayBufferLike | Blob | ArrayBufferView> = [];
  public readonly firstBinaryFrame: Promise<ArrayBuffer>;
  public readonly firstCloseReport: Promise<void>;
  private resolveFirstBinaryFrame!: (frame: ArrayBuffer) => void;
  private resolveFirstCloseReport!: () => void;

  public constructor() {
    this.firstBinaryFrame = new Promise(resolve => {
      this.resolveFirstBinaryFrame = resolve;
    });
    this.firstCloseReport = new Promise(resolve => {
      this.resolveFirstCloseReport = resolve;
    });
  }

  public open(): void {
    this.readyState = 1;
    this.onopen?.(new Event('open'));
  }

  public message(data: string | ArrayBuffer | ArrayBufferView): void {
    this.onmessage?.(new MessageEvent('message', { data }));
  }

  public drop(code = 1006, reason = ''): void {
    this.readyState = 3;
    this.onclose?.(new CloseEvent('close', { code, reason, wasClean: code === 1000 }));
  }

  public send(data: string | ArrayBufferLike | Blob | ArrayBufferView): void {
    this.sent.push(data);
    if (data instanceof ArrayBuffer) {
      this.resolveFirstBinaryFrame(data);
    } else if (typeof data === 'string' && data.startsWith('{')) {
      this.resolveFirstCloseReport();
    }
  }

  public close(code = 1000, reason = ''): void {
    this.drop(code, reason);
  }
}

describe('UsbDeviceRelayService', () => {
  let service: UsbDeviceRelayService | null = null;
  let socket: FakeSocket;
  let tabChannel: FakeTabChannel;
  let socketUrl: string | undefined;
  let manager: { requestDevice: jasmine.Spy };
  let webUsbDeviceManager: AdbDaemonWebUsbDeviceManager | undefined;
  let device: {
    connect: jasmine.Spy;
    raw: { close: jasmine.Spy };
  };
  let packetController: ReadableStreamDefaultController<AdbPacketData>;
  let receivedPackets: AdbPacketInit[];
  let packetWriteCompleted: Promise<void>;
  let resolvePacketWrite!: () => void;
  let writeError: Error | null;
  let closeLog: jasmine.Spy;

  beforeEach(() => {
    tabChannel = new FakeTabChannel();
    service = null;
    socket = new FakeSocket();
    socketUrl = undefined;
    receivedPackets = [];
    writeError = null;
    packetWriteCompleted = new Promise(resolve => {
      resolvePacketWrite = resolve;
    });

    const readable = new ReadableStream<AdbPacketData>({
      start(controller) {
        packetController = controller;
      }
    });
    const writable = new WritableStream<Consumable<AdbPacketInit>>({
      write(packet) {
        if (writeError) {
          throw writeError;
        }
        receivedPackets.push(packet.value);
        resolvePacketWrite();
      }
    });
    device = {
      connect: jasmine.createSpy('connect').and.resolveTo({ readable, writable }),
      raw: { close: jasmine.createSpy('close').and.resolveTo(undefined) }
    };
    manager = {
      requestDevice: jasmine.createSpy('requestDevice').and.resolveTo(device)
    };
    webUsbDeviceManager = manager as unknown as AdbDaemonWebUsbDeviceManager;

    TestBed.configureTestingModule({
      providers: [
        UsbDeviceRelayService,
        {
          provide: WEBUSB_DEVICE_MANAGER,
          useFactory: () => webUsbDeviceManager
        },
        { provide: PHONE_TAB_CHANNEL, useFactory: () => tabChannel },
        {
          provide: DEVICE_BRIDGE_SOCKET_FACTORY,
          useValue: (url: string) => {
            socketUrl = url;
            return socket as unknown as WebSocket;
          }
        }
      ]
    });
    closeLog = spyOn(TestBed.inject(LoggerService), 'warn');
  });

  afterEach(async () => {
    if (service) {
      await service.disconnect();
    }
    TestBed.resetTestingModule();
  });

  it('reports unsupported browsers without requesting USB permission', async () => {
    webUsbDeviceManager = undefined;
    service = TestBed.inject(UsbDeviceRelayService);

    await service.connect();

    expect(service.isSupported()).toBeFalse();
    expect(service.state().error).toContain('WebUSB is unavailable');
    expect(manager.requestDevice).not.toHaveBeenCalled();
  });

  it('goes back to no phone, with no error, when the chooser is closed without a choice (OCR F3)', async () => {
    manager.requestDevice.and.rejectWith(new DOMException('No device selected.', 'NotFoundError'));
    service = TestBed.inject(UsbDeviceRelayService);

    await service.connect();

    expect(service.state()).toEqual({ status: 'idle', serial: null, sessionId: null, error: null });
    expect(socketUrl).toBeUndefined();
  });

  it('is attaching from the end of the chooser until the bridge reports the phone (OCR F3)', async () => {
    service = TestBed.inject(UsbDeviceRelayService);
    const connecting = service.connect();
    expect(service.attaching()).toBeFalse();
    await flushMicrotasks();
    socket.open();
    await connecting;

    expect(service.state().status).toBe('connecting');
    expect(service.attaching()).toBeTrue();

    socket.message(JSON.stringify({ type: 'session_leased', session_id: 'lease-1', expires_in_seconds: 300 }));
    socket.message(JSON.stringify({ type: 'device_attached', serial: 'R58M123' }));
    expect(service.attaching()).toBeFalse();
  });

  it('tells other tabs which phone it holds, and that it let go (OCR F3)', async () => {
    service = TestBed.inject(UsbDeviceRelayService);
    const connecting = service.connect();
    await flushMicrotasks();
    socket.open();
    await connecting;
    socket.message(JSON.stringify({ type: 'session_leased', session_id: 'lease-1', expires_in_seconds: 300 }));
    socket.message(JSON.stringify({ type: 'device_attached', serial: 'R58M123' }));
    expect(tabChannel.posted).toContain(jasmine.objectContaining({ type: 'held', serial: 'R58M123' }));

    await service.disconnect();
    expect(tabChannel.posted.at(-1)).toEqual(jasmine.objectContaining({ type: 'released' }));
  });

  it('lets go and shows Disconnected when another tab takes the phone over (OCR F3)', async () => {
    service = TestBed.inject(UsbDeviceRelayService);
    const connecting = service.connect();
    await flushMicrotasks();
    socket.open();
    await connecting;
    socket.message(JSON.stringify({ type: 'session_leased', session_id: 'lease-1', expires_in_seconds: 300 }));
    socket.message(JSON.stringify({ type: 'device_attached', serial: 'R58M123' }));

    const me = tabChannel.posted.find((m) => m.type === 'held')!.tab;
    // A request aimed at some other tab, or at another phone, leaves this tab connected.
    tabChannel.fromOtherTab({ type: 'release', target: 'someone-else', serial: 'R58M123' });
    tabChannel.fromOtherTab({ type: 'release', target: me, serial: 'another-phone' });
    await flushMicrotasks();
    expect(service.state().status).toBe('connected');

    tabChannel.fromOtherTab({ type: 'release', target: me, serial: 'R58M123' });
    await flushMicrotasks();

    expect(service.state().status).toBe('dropped');
    expect(service.state().error).toBe('This phone is now used in another tab.');
    expect(tabChannel.posted.at(-1)).toEqual(jasmine.objectContaining({ type: 'released' }));
  });

  it('knows when another tab holds a phone', () => {
    service = TestBed.inject(UsbDeviceRelayService);
    tabChannel.fromOtherTab({ type: 'held', serial: '127.0.0.1:41003' });
    expect(service.heldInAnotherTab()).toEqual({ serial: '127.0.0.1:41003' });
  });

  it('reports a denied USB permission request', async () => {
    manager.requestDevice.and.rejectWith(new DOMException('Denied', 'NotAllowedError'));
    service = TestBed.inject(UsbDeviceRelayService);

    await service.connect();

    expect(service.state().error).toContain('USB permission was not granted');
    expect(socketUrl).toBeUndefined();
  });

  it('explains when another ADB process has claimed the device interface', async () => {
    const busy = new AdbDaemonWebUsbDevice.DeviceBusyError(
      new DOMException('Unable to claim interface.', 'NetworkError')
    );
    device.connect.and.rejectWith(busy);
    service = TestBed.inject(UsbDeviceRelayService);

    await service.connect();

    expect(service.state().error).toBe(
      'Another program on this computer is using the phone (adb, Android Studio, scrcpy). ' +
      'Quit it or run adb kill-server, unplug and replug, then retry.'
    );
    expect(device.raw.close).toHaveBeenCalled();
  });

  it('explains a WebUSB network error as a competing USB program', async () => {
    device.connect.and.rejectWith(new DOMException('Unable to claim interface.', 'NetworkError'));
    service = TestBed.inject(UsbDeviceRelayService);

    await service.connect();

    expect(service.state().error).toBe(
      'Another program on this computer is using the phone (adb, Android Studio, scrcpy). ' +
      'Quit it or run adb kill-server, unplug and replug, then retry.'
    );
  });

  it('explains an interface claim message even with a different browser error name', async () => {
    device.connect.and.rejectWith(new DOMException('Unable to claim interface.', 'InvalidStateError'));
    service = TestBed.inject(UsbDeviceRelayService);

    await service.connect();

    expect(service.state().error).toContain('Another program on this computer is using the phone');
  });

  it('shows transfer details instead of USB contention for a mid-session network error', async () => {
    service = TestBed.inject(UsbDeviceRelayService);
    const connecting = service.connect();
    await flushMicrotasks();
    socket.open();
    await connecting;

    packetController.error(new DOMException('USB transfer failed.', 'NetworkError'));
    await new Promise<void>(resolve => setTimeout(resolve, 0));
    await flushMicrotasks();

    expect(service.state().error).toBe(
      'Could not connect the phone. Check its cable and USB Debugging, then retry.\n' +
      'Details: NetworkError: USB transfer failed.'
    );
  });

  it('shows and logs unexpected WebUSB error details', async () => {
    const originalError = new DOMException('The interface is unavailable.', 'InvalidStateError');
    const consoleError = spyOn(console, 'error');
    device.connect.and.rejectWith(originalError);
    service = TestBed.inject(UsbDeviceRelayService);

    await service.connect();

    expect(service.state().error).toBe(
      'Could not connect the phone. Check its cable and USB Debugging, then retry.\n' +
      'Details: InvalidStateError: The interface is unavailable.'
    );
    expect(consoleError).toHaveBeenCalledWith('Device bridge connection failed:', jasmine.objectContaining({ name: originalError.name, message: originalError.message }));
  });

  it('relays complete ADB packets in both directions and shows the attached serial', async () => {
    service = TestBed.inject(UsbDeviceRelayService);
    const connecting = service.connect();
    await flushMicrotasks();
    const expectedProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    expect(socketUrl).toBe(
      `${expectedProtocol}//${window.location.host}/api/device-bridge/session`
    );
    expect(device.connect).toHaveBeenCalled();

    socket.open();
    await connecting;
    socket.message(JSON.stringify({
      type: 'session_leased',
      session_id: 'lease-1',
      expires_in_seconds: 300
    }));
    socket.message(JSON.stringify({ type: 'device_attached', serial: 'R58M123' }));
    expect(service.state()).toEqual({ status: 'connected', serial: 'R58M123', sessionId: 'lease-1', error: null });

    const devicePacket = {
      command: AdbCommand.Okay,
      arg0: 1,
      arg1: 2,
      payload: new Uint8Array([3, 4]),
      checksum: 7,
      magic: (AdbCommand.Okay ^ 0xffffffff) >>> 0
    } as AdbPacketData;
    packetController.enqueue(devicePacket);
    const outgoingFrame = await socket.firstBinaryFrame;
    expect(Array.from(new Uint8Array(outgoingFrame))).toEqual(
      Array.from(AdbPacket.serialize(devicePacket as AdbPacketInit))
    );

    const bridgePacket = AdbPacket.serialize({
      command: AdbCommand.Write,
      arg0: 5,
      arg1: 6,
      checksum: 0,
      magic: (AdbCommand.Write ^ 0xffffffff) >>> 0,
      payload: new Uint8Array([7, 8])
    });
    socket.message(bridgePacket);
    await packetWriteCompleted;
    expect(receivedPackets.length).toBe(1);
    expect(receivedPackets[0]).toEqual(jasmine.objectContaining({
      command: AdbCommand.Write,
      arg0: 5,
      arg1: 6,
      checksum: 0,
      payload: new Uint8Array([7, 8])
    }));

    await service.disconnect();
    expect(JSON.parse(socket.sent.at(-1) as string)).toEqual(jasmine.objectContaining({
      type: 'client_close', reason: 'manual_disconnect', usb_error: null
    }));
    expect(service.state().status).toBe('idle');
  });

  it('rejects malformed binary frames and closes the relay', async () => {
    service = TestBed.inject(UsbDeviceRelayService);
    const connecting = service.connect();
    await flushMicrotasks();
    socket.open();
    await connecting;
    socket.message(JSON.stringify({
      type: 'session_leased',
      session_id: 'lease-1',
      expires_in_seconds: 300
    }));

    const malformed = AdbPacket.serialize({
      command: AdbCommand.Write,
      arg0: 1,
      arg1: 2,
      checksum: 0,
      magic: 0,
      payload: new Uint8Array([1])
    });
    socket.message(malformed);
    await flushMicrotasks();

    expect(service.state().error).toContain('invalid ADB data');
    expect(receivedPackets.length).toBe(0);
  });

  it('cleans up the WebUSB connection after an unexpected bridge drop', async () => {
    service = TestBed.inject(UsbDeviceRelayService);
    const connecting = service.connect();
    await flushMicrotasks();
    socket.open();
    await connecting;

    socket.drop();
    await flushMicrotasks();

    expect(service.state().error).toContain('connection dropped');
    expect(service.state().serial).toBeNull();
    // It never reported an attached phone, so this is a failed connect, not a dropped phone.
    expect(service.state().status).toBe('error');
  });

  it('reports a phone that was attached and then lost as dropped, and can reconnect', async () => {
    service = TestBed.inject(UsbDeviceRelayService);
    const connecting = service.connect();
    await flushMicrotasks();
    socket.open();
    await connecting;
    socket.message(JSON.stringify({ type: 'session_leased', session_id: 'lease-1', expires_in_seconds: 300 }));
    socket.message(JSON.stringify({ type: 'device_attached', serial: 'R58M123' }));
    expect(service.state().sessionId).toBe('lease-1');

    socket.drop();
    await flushMicrotasks();

    expect(service.state()).toEqual({
      status: 'dropped',
      serial: null,
      sessionId: null,
      error: 'The device bridge connection dropped. Connect the phone again.'
    });

    // Dropped is not "still connected": a new connect goes back to the USB chooser.
    void service.connect();
    await flushMicrotasks();
    expect(manager.requestDevice).toHaveBeenCalledTimes(2);
  });

  it('warns before leaving while the bridge is active', async () => {
    let beforeUnloadHandler: ((event: BeforeUnloadEvent) => void) | undefined;
    const addEventListener = window.addEventListener.bind(window);
    spyOn(window, 'addEventListener').and.callFake((
      type: string,
      listener: EventListenerOrEventListenerObject,
      options?: boolean | AddEventListenerOptions
    ) => {
      if (type === 'beforeunload') {
        beforeUnloadHandler = listener as unknown as (event: BeforeUnloadEvent) => void;
      } else {
        addEventListener(type, listener, options);
      }
    });

    service = TestBed.inject(UsbDeviceRelayService);
    const connecting = service.connect();
    await flushMicrotasks();
    socket.open();
    await connecting;
    socket.message(JSON.stringify({
      type: 'session_leased',
      session_id: 'lease-1',
      expires_in_seconds: 300
    }));
    socket.message(JSON.stringify({ type: 'device_attached', serial: 'R58M123' }));

    const event = new Event('beforeunload', { cancelable: true });
    expect(beforeUnloadHandler).toBeDefined();
    beforeUnloadHandler?.(event as BeforeUnloadEvent);

    expect(event.defaultPrevented).toBeTrue();
  });

  async function openRelay(): Promise<void> {
    service = TestBed.inject(UsbDeviceRelayService);
    const connecting = service.connect();
    await flushMicrotasks();
    socket.open();
    await connecting;
    socket.message(JSON.stringify({
      type: 'session_leased', session_id: 'lease-1', expires_in_seconds: 300
    }));
    socket.message(JSON.stringify({ type: 'device_attached', serial: 'R58M123' }));
  }

  function expectClose(reason: string, usbError: string | null = null): void {
    expect(closeLog).toHaveBeenCalledWith('Device bridge closing:', jasmine.objectContaining({
      reason, usb_error: usbError
    }));
    expect(JSON.parse(socket.sent.at(-1) as string)).toEqual(jasmine.objectContaining({
      type: 'client_close', reason, usb_error: usbError
    }));
    expect(socket.readyState).toBe(3);
  }

  it('reports manual disconnect before closing and cleans up only once', async () => {
    await openRelay();
    const close = spyOn(socket, 'close').and.callFake(() => {
      expect(JSON.parse(socket.sent.at(-1) as string).reason).toBe('manual_disconnect');
      socket.drop(1000, 'manual_disconnect');
    });
    await service!.disconnect();
    await service!.disconnect();
    expectClose('manual_disconnect');
    expect(close).toHaveBeenCalledTimes(1);
    expect(closeLog.calls.allArgs().filter(args => args[0] === 'Device bridge closing:').length).toBe(1);
  });

  it('reports service destruction separately from manual disconnect', async () => {
    await openRelay();
    service!.ngOnDestroy();
    await flushMicrotasks();
    expectClose('service_destroyed');
  });

  it('logs USB setup errors even before a bridge socket exists', async () => {
    device.connect.and.rejectWith(new DOMException('connect failed', 'NetworkError'));
    service = TestBed.inject(UsbDeviceRelayService);
    await service.connect();
    expect(closeLog).toHaveBeenCalledWith('Device bridge closing:', jasmine.objectContaining({
      reason: 'connect_error', usb_error: 'NetworkError: connect failed'
    }));
    expect(device.raw.close).toHaveBeenCalled();
  });

  it('logs socket errors during opening', async () => {
    service = TestBed.inject(UsbDeviceRelayService);
    const connecting = service.connect();
    await flushMicrotasks();
    socket.onerror?.(new Event('error'));
    await connecting;
    expect(closeLog).toHaveBeenCalledWith('Device bridge closing:', jasmine.objectContaining({
      reason: 'socket_error'
    }));
    expect(socket.readyState).toBe(3);
  });

  it('logs a socket close before opening with its received close code', async () => {
    service = TestBed.inject(UsbDeviceRelayService);
    const connecting = service.connect();
    await flushMicrotasks();
    socket.drop(1008, 'not_signed_in');
    await connecting;
    expect(closeLog).toHaveBeenCalledWith('Device bridge socket closed:', jasmine.objectContaining({
      code: 1008, reason: 'not_signed_in'
    }));
    expect(closeLog).toHaveBeenCalledWith('Device bridge closing:', jasmine.objectContaining({
      reason: 'socket_closed'
    }));
  });

  it('reports page unload without closing for a cancelled beforeunload warning', async () => {
    const handlers = new Map<string, EventListener>();
    spyOn(window, 'addEventListener').and.callFake((
      type: string, listener: EventListenerOrEventListenerObject
    ) => {
      handlers.set(type, listener as EventListener);
    });
    await openRelay();
    handlers.get('beforeunload')!(new Event('beforeunload', { cancelable: true }));
    expect(socket.readyState).toBe(1);
    handlers.get('pagehide')!(new Event('pagehide'));
    await flushMicrotasks();
    expectClose('page_unload');
  });

  it('logs the received WebSocket close code and reason without sending on a closed socket', async () => {
    await openRelay();
    socket.drop(1011, 'upstream reset');
    await flushMicrotasks();
    expect(closeLog).toHaveBeenCalledWith('Device bridge socket closed:', jasmine.objectContaining({
      code: 1011, reason: 'upstream reset', was_clean: false
    }));
    expect(closeLog).toHaveBeenCalledWith('Device bridge closing:', jasmine.objectContaining({
      reason: 'socket_closed', usb_error: null
    }));
    expect(socket.sent).toEqual([]);
  });

  it('reports a socket error before closing', async () => {
    await openRelay();
    socket.onerror?.(new Event('error'));
    await flushMicrotasks();
    expectClose('socket_error');
  });

  it('reports invalid control messages before closing', async () => {
    await openRelay();
    socket.message('{');
    await flushMicrotasks();
    expectClose('protocol_error');
  });

  it('reports a bridge rejection before closing', async () => {
    await openRelay();
    socket.message(JSON.stringify({ type: 'error' }));
    await flushMicrotasks();
    expectClose('bridge_rejected');
  });

  it('reports malformed ADB frames separately from USB errors', async () => {
    await openRelay();
    socket.message(new Uint8Array([1]).buffer);
    await flushMicrotasks();
    expectClose('protocol_error');
  });

  it('reports a USB transfer-in error before closing', async () => {
    await openRelay();
    packetController.error(new DOMException('transferIn failed', 'NetworkError'));
    await socket.firstCloseReport;
    await flushMicrotasks();
    expectClose('usb_read_error', 'NetworkError: transferIn failed');
  });

  it('fault-injects a USB transfer-out error without blaming the server packet', async () => {
    await openRelay();
    writeError = new DOMException('transferOut failed', 'NetworkError');
    socket.message(AdbPacket.serialize({
      command: AdbCommand.Write, arg0: 1, arg1: 2, checksum: 0,
      magic: (AdbCommand.Write ^ 0xffffffff) >>> 0, payload: new Uint8Array([3])
    }));
    await socket.firstCloseReport;
    await flushMicrotasks();
    expectClose('usb_write_error', 'NetworkError: transferOut failed');
    expect(service!.state().error).not.toContain('invalid ADB data');
  });

  it('reports USB end-of-stream as a phone disconnect', async () => {
    await openRelay();
    packetController.close();
    await socket.firstCloseReport;
    await flushMicrotasks();
    expectClose('usb_disconnected');
  });

  it('bounds and redacts USB diagnostics before sending them to the server', async () => {
    await openRelay();
    packetController.error(new Error(`token=synthetic-secret ${'x'.repeat(600)}`));
    await socket.firstCloseReport;
    const report = JSON.parse(socket.sent.at(-1) as string);
    expect(report.usb_error.length).toBeLessThanOrEqual(512);
    expect(report.usb_error).toContain('[REDACTED]');
    expect(report.usb_error).not.toContain('synthetic-secret');
  });

  it('reports attachment timeout before closing', async () => {
    jasmine.clock().install();
    try {
      service = TestBed.inject(UsbDeviceRelayService);
      const connecting = service.connect();
      await flushMicrotasks();
      socket.open();
      await connecting;
      jasmine.clock().tick(30_000);
      await flushMicrotasks();
      expectClose('attachment_timeout');
    } finally {
      jasmine.clock().uninstall();
    }
  });

  it('logs an opening timeout even when a control frame cannot be sent', async () => {
    jasmine.clock().install();
    try {
      service = TestBed.inject(UsbDeviceRelayService);
      const connecting = service.connect();
      await flushMicrotasks();
      jasmine.clock().tick(15_000);
      await connecting;
      expect(closeLog).toHaveBeenCalledWith('Device bridge closing:', jasmine.objectContaining({
        reason: 'socket_open_timeout'
      }));
      expect(socket.sent).toEqual([]);
    } finally {
      jasmine.clock().uninstall();
    }
  });

  it('does not close an attached bridge just because the tab becomes hidden', async () => {
    jasmine.clock().install();
    try {
      await openRelay();
      spyOnProperty(document, 'visibilityState', 'get').and.returnValue('hidden');
      document.dispatchEvent(new Event('visibilitychange'));
      jasmine.clock().tick(142_000);
      await flushMicrotasks();
      expect(service!.state().status).toBe('connected');
      expect(socket.readyState).toBe(1);
      await service!.disconnect();
      expect(JSON.parse(socket.sent.at(-1) as string).visibility_state).toBe('hidden');
    } finally {
      jasmine.clock().uninstall();
    }
  });
});

async function flushMicrotasks(): Promise<void> {
  for (let index = 0; index < 8; index += 1) {
    await Promise.resolve();
  }
}
