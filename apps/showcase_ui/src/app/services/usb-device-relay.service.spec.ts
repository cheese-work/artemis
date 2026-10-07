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
import { PHONE_TAB_CHANNEL, TabChannel } from './phone-tab.service';
import {
  DEVICE_BRIDGE_SOCKET_FACTORY,
  UsbDeviceRelayService,
  WEBUSB_DEVICE_MANAGER
} from './usb-device-relay.service';

/** A tab channel that records what this tab says and lets a spec speak as another tab. */
class FakeTabChannel implements TabChannel {
  public posted: { type: string; serial?: string }[] = [];
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
  private resolveFirstBinaryFrame!: (frame: ArrayBuffer) => void;

  public constructor() {
    this.firstBinaryFrame = new Promise(resolve => {
      this.resolveFirstBinaryFrame = resolve;
    });
  }

  public open(): void {
    this.readyState = 1;
    this.onopen?.(new Event('open'));
  }

  public message(data: string | ArrayBuffer | ArrayBufferView): void {
    this.onmessage?.(new MessageEvent('message', { data }));
  }

  public drop(): void {
    this.readyState = 3;
    this.onclose?.(new CloseEvent('close'));
  }

  public send(data: string | ArrayBufferLike | Blob | ArrayBufferView): void {
    this.sent.push(data);
    if (data instanceof ArrayBuffer) {
      this.resolveFirstBinaryFrame(data);
    }
  }

  public close(): void {
    this.readyState = 3;
    this.onclose?.(new CloseEvent('close'));
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

  beforeEach(() => {
    tabChannel = new FakeTabChannel();
    service = null;
    socket = new FakeSocket();
    socketUrl = undefined;
    receivedPackets = [];
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

    tabChannel.fromOtherTab({ type: 'release' });
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
    expect(consoleError).toHaveBeenCalledWith(originalError);
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
    expect(socket.sent.at(-1)).toBe('close');
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
});

async function flushMicrotasks(): Promise<void> {
  for (let index = 0; index < 8; index += 1) {
    await Promise.resolve();
  }
}
