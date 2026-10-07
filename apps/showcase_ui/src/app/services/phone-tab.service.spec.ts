import { Injector, runInInjectionContext } from '@angular/core';
import { fakeAsync, tick } from '@angular/core/testing';
import { PHONE_TAB_CHANNEL, PhoneTabService, TabChannel } from './phone-tab.service';

/** Two in-memory channels that deliver to each other, like two tabs on one BroadcastChannel. */
function pair(): [TabChannel, TabChannel] {
  const listeners: ((event: MessageEvent) => void)[][] = [[], []];
  const make = (self: 0 | 1): TabChannel => ({
    postMessage: (data) => listeners[1 - self].forEach((l) => queueMicrotask(() => l({ data } as MessageEvent))),
    addEventListener: (_t, l) => listeners[self].push(l),
    removeEventListener: (_t, l) => listeners[self].splice(listeners[self].indexOf(l), 1),
    close: () => undefined
  });
  return [make(0), make(1)];
}

/** One tab: a service of its own on the given channel. */
function tab(channel: TabChannel | null): PhoneTabService {
  const injector = Injector.create({ providers: [{ provide: PHONE_TAB_CHANNEL, useValue: channel }] });
  return runInInjectionContext(injector, () => new PhoneTabService());
}

describe('PhoneTabService', () => {
  function create(channels: [TabChannel, TabChannel]): [PhoneTabService, PhoneTabService] {
    return [tab(channels[0]), tab(channels[1])];
  }

  it('tells a second tab which phone the first holds, and clears it on release', fakeAsync(() => {
    const [a, b] = create(pair());
    a.announceHeld('127.0.0.1:41003');
    tick();
    expect(b.heldElsewhere()).toEqual({ serial: '127.0.0.1:41003' });
    expect(a.heldElsewhere()).toBeNull();

    a.announceReleased();
    tick();
    expect(b.heldElsewhere()).toBeNull();
  }));

  it('tells a tab that opens later, by asking who holds a phone', fakeAsync(() => {
    const channels = pair();
    const a = tab(channels[0]);
    a.announceHeld('S');
    tick();
    const b = tab(channels[1]);
    tick();
    expect(b.heldElsewhere()).toEqual({ serial: 'S' });
  }));

  it('asks the holder to let go and resolves when it has', fakeAsync(() => {
    const [a, b] = create(pair());
    a.onReleaseRequest(() => a.announceReleased());
    a.announceHeld('S');
    tick();
    let done = false;
    void b.requestRelease().then(() => (done = true));
    tick();
    expect(done).toBeTrue();
    expect(b.heldElsewhere()).toBeNull();
  }));

  it('stops waiting if the holder never answers', fakeAsync(() => {
    const [a, b] = create(pair());
    a.announceHeld('S'); // holder registered no handler: it never lets go
    tick();
    let done = false;
    void b.requestRelease().then(() => (done = true));
    tick(1000);
    expect(done).toBeFalse();
    tick(600);
    expect(done).toBeTrue();
  }));

  it('does not wait when no other tab holds a phone', fakeAsync(() => {
    const [, b] = create(pair());
    let done = false;
    void b.requestRelease().then(() => (done = true));
    tick();
    expect(done).toBeTrue();
  }));

  it('works with no BroadcastChannel at all', () => {
    const service = tab(null);
    service.announceHeld('S');
    expect(service.heldElsewhere()).toBeNull();
  });
});
