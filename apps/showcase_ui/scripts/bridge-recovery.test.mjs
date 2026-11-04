import assert from 'node:assert/strict';
import { test } from 'node:test';
import { build } from 'esbuild';

const result = await build({
  stdin: {
    contents: `
      import '@angular/compiler';
      import { Injector, runInInjectionContext, signal } from '@angular/core';
      import { DOCUMENT } from '@angular/common';
      import { LoggerService } from './src/app/services/logger.service';
      import { PhoneTabService } from './src/app/services/phone-tab.service';
      import { UsbDeviceRelayService, WEBUSB_DEVICE_MANAGER,
        DEVICE_BRIDGE_SOCKET_FACTORY } from './src/app/services/usb-device-relay.service';
      export function createRelay(manager, socketFactory) {
        const injector = Injector.create({ providers: [
          { provide: DOCUMENT, useValue: {
            baseURI: 'http://127.0.0.1/', location: { href: 'http://127.0.0.1/' },
            defaultView: null, visibilityState: 'visible'
          } },
          { provide: LoggerService, useValue: { warn() {} } },
          { provide: PhoneTabService, useValue: {
            heldElsewhere: signal(null), onReleaseRequest() {},
            announceHeld() {}, announceReleased() {}
          } },
          { provide: WEBUSB_DEVICE_MANAGER, useValue: manager },
          { provide: DEVICE_BRIDGE_SOCKET_FACTORY, useValue: socketFactory }
        ] });
        return runInInjectionContext(injector, () => new UsbDeviceRelayService());
      }
    `,
    resolveDir: process.cwd(),
    sourcefile: 'bridge-recovery-regression.ts'
  },
  bundle: true,
  format: 'esm',
  platform: 'node',
  target: 'esnext',
  write: false
});
const { createRelay } = await import(
  `data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text).toString('base64')}`
);

class FakeSocket {
  readyState = 0;
  sent = [];
  open() { this.readyState = 1; this.onopen?.({}); }
  message(data) { this.onmessage?.({ data: JSON.stringify(data) }); }
  send(data) { this.sent.push(data); }
  drop(code) {
    this.readyState = 3;
    this.onclose?.({ code, reason: '', wasClean: false });
  }
  close() { this.drop(1000); }
  attach() {
    this.message({ type: 'session_leased', session_id: 'lease-1',
      expires_in_seconds: 300, reconnect_token: 'resume-secret' });
    this.message({ type: 'device_attached', serial: 'fake-phone' });
  }
}

const flush = async () => {
  for (let i = 0; i < 20; i++) await Promise.resolve();
};

test('a recovered 1005/1006 relay survives the previous attachment timeout', async t => {
  t.mock.timers.enable({ apis: ['setTimeout', 'Date'] });
  let closed = 0;
  let selected = 0;
  const device = {
    raw: { async close() { closed++; } },
    async connect() {
      return { readable: new ReadableStream(), writable: new WritableStream() };
    }
  };
  const sockets = [new FakeSocket(), new FakeSocket(), new FakeSocket()];
  const queued = [...sockets];
  const service = createRelay({ async requestDevice() { selected++; return device; } },
    () => queued.shift());
  try {
    const connecting = service.connect();
    await flush();
    sockets[0].open();
    await connecting;
    sockets[0].attach();
    sockets[0].drop(1005);
    t.mock.timers.tick(500);
    await flush();
    sockets[1].open();
    await flush();
    sockets[1].drop(1006);
    t.mock.timers.tick(5000);
    await flush();
    t.mock.timers.tick(1000);
    await flush();
    sockets[2].open();
    sockets[2].attach();
    await flush();
    assert.equal(service.state().status, 'connected');
    t.mock.timers.tick(30_000);
    await flush();
    assert.equal(service.state().status, 'connected');
    assert.equal(closed, 0);
    assert.equal(selected, 1);
  } finally {
    await service.disconnect();
  }
});
