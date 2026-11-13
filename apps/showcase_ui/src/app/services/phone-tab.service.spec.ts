import { Injector, runInInjectionContext } from '@angular/core';
import { fakeAsync, tick } from '@angular/core/testing';
import { PHONE_TAB_CHANNEL, PhoneTabService, TabChannel } from './phone-tab.service';

/** In-memory channels that each deliver to every other one, like tabs on one BroadcastChannel. */
function bus(count: number): TabChannel[] {
  const listeners: ((event: MessageEvent) => void)[][] = Array.from({ length: count }, () => []);
  return listeners.map((own, self) => ({
    postMessage: (data) =>
      listeners.forEach((all, other) => {
        if (other !== self) all.forEach((l) => queueMicrotask(() => l({ data } as MessageEvent)));
      }),
    addEventListener: (_t, l) => own.push(l),
    removeEventListener: (_t, l) => own.splice(own.indexOf(l), 1),
    close: () => undefined
  }));
}
function pair(): [TabChannel, TabChannel] {
  const [a, b] = bus(2);
  return [a, b];
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
    void b.requestRelease('S').then(() => (done = true));
    tick();
    expect(done).toBeTrue();
    expect(b.heldElsewhere()).toBeNull();
  }));

  it('stops waiting if the holder never answers', fakeAsync(() => {
    const [a, b] = create(pair());
    a.announceHeld('S'); // holder registered no handler: it never lets go
    tick();
    let done = false;
    void b.requestRelease('S').then(() => (done = true));
    tick(1000);
    expect(done).toBeFalse();
    tick(600);
    expect(done).toBeTrue();
  }));

  it('does not wait when no other tab holds a phone', fakeAsync(() => {
    const [, b] = create(pair());
    let done = false;
    void b.requestRelease('S').then(() => (done = true));
    tick();
    expect(done).toBeTrue();
  }));

  it('works with no BroadcastChannel at all', () => {
    const service = tab(null);
    service.announceHeld('S');
    expect(service.heldElsewhere()).toBeNull();
  });

  describe('three tabs, two phones (OCR F5)', () => {
    /** A holds phone A, B holds phone B, C holds nothing and sees both. */
    function threeTabs() {
      const [chA, chB, chC] = bus(3);
      const a = tab(chA);
      const b = tab(chB);
      const c = tab(chC);
      const releasedA = jasmine.createSpy('releaseA').and.callFake(() => a.announceReleased());
      const releasedB = jasmine.createSpy('releaseB').and.callFake(() => b.announceReleased());
      a.onReleaseRequest(releasedA);
      b.onReleaseRequest(releasedB);
      a.announceHeld('phone-A');
      b.announceHeld('phone-B');
      return { a, b, c, releasedA, releasedB };
    }

    it('asks only the tab that holds the requested phone to let go', fakeAsync(() => {
      const { c, releasedA, releasedB } = threeTabs();
      tick();

      void c.requestRelease('phone-B');
      tick();

      expect(releasedB).toHaveBeenCalledTimes(1);
      expect(releasedA).not.toHaveBeenCalled();
    }));

    it('ignores an acknowledgement from a tab that was not asked', fakeAsync(() => {
      const [chA, chB, chC] = bus(3);
      const a = tab(chA);
      const b = tab(chB); // holds phone B but never answers
      const c = tab(chC);
      a.announceHeld('phone-A');
      b.announceHeld('phone-B');
      tick();
      let done = false;
      void c.requestRelease('phone-B').then(() => (done = true));
      tick();

      a.announceReleased(); // an unrelated tab lets go of its own phone meanwhile
      tick();
      expect(done).toBeFalse();

      tick(1600); // only the timeout ends the wait
      expect(done).toBeTrue();
    }));

    it('keeps seeing the other holder after one of two holders lets go', fakeAsync(() => {
      const { a, c } = threeTabs();
      tick();
      expect(c.heldElsewhere()).toEqual({ serial: 'phone-B' });
      a.announceReleased();
      tick();
      expect(c.heldElsewhere()).toEqual({ serial: 'phone-B' });
    }));
  });
});
