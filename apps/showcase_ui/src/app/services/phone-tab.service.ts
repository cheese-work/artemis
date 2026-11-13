import { inject, Injectable, InjectionToken, OnDestroy, signal } from '@angular/core';

/** The slice of BroadcastChannel this service uses, so tests can pair two tabs in memory. */
export interface TabChannel {
  postMessage(message: unknown): void;
  addEventListener(type: 'message', listener: (event: MessageEvent) => void): void;
  removeEventListener(type: 'message', listener: (event: MessageEvent) => void): void;
  close(): void;
}

export const PHONE_TAB_CHANNEL = new InjectionToken<TabChannel | null>('PHONE_TAB_CHANNEL', {
  providedIn: 'root',
  factory: () => (typeof BroadcastChannel === 'undefined' ? null : new BroadcastChannel('smartqa-phone'))
});

type TabMessage =
  | { type: 'query'; tab: string }
  | { type: 'held'; tab: string; serial: string }
  | { type: 'released'; tab: string }
  /** Asks the tab `target`, which holds `serial`, to let go. Every other tab ignores it. */
  | { type: 'release'; tab: string; target: string; serial: string };

const TAKE_OVER_TIMEOUT_MS = 1500;

/**
 * One browser, several tabs, one phone: only one tab can hold a phone's USB interface. Tabs
 * tell each other who holds it, so a second tab can say "Connected in another tab" and offer
 * "Use here", which asks the holder to let go.
 */
@Injectable({ providedIn: 'root' })
export class PhoneTabService implements OnDestroy {
  private readonly channel = inject(PHONE_TAB_CHANNEL);
  private readonly tab = Math.random().toString(36).slice(2);
  private held: string | null = null;
  private releaseHandler: (() => void) | null = null;
  /** Who is waiting for which tab to let go. */
  private readonly waiting = new Map<string, Set<() => void>>();
  /** Every other tab that holds a phone, and which. The newest announcement is last. */
  private readonly holders = new Map<string, string>();

  /** A phone another tab of this browser holds (the newest announced); null when none does. */
  public readonly heldElsewhere = signal<{ serial: string } | null>(null);

  private publishHeldElsewhere(): void {
    const newest = [...this.holders.values()].at(-1);
    this.heldElsewhere.set(newest === undefined ? null : { serial: newest });
  }

  private readonly onMessage = (event: MessageEvent): void => {
    const message = event.data as TabMessage | null;
    if (!message || message.tab === this.tab) return;
    switch (message.type) {
      case 'query':
        if (this.held) this.post({ type: 'held', tab: this.tab, serial: this.held });
        break;
      case 'held':
        this.holders.delete(message.tab); // a tab that switched phones becomes the newest holder
        this.holders.set(message.tab, message.serial);
        this.publishHeldElsewhere();
        break;
      case 'released':
        this.holders.delete(message.tab);
        this.publishHeldElsewhere();
        // Only the tab that was asked ends a wait; a stranger letting go of its own phone does not.
        this.waiting.get(message.tab)?.forEach((resolve) => resolve());
        break;
      case 'release':
        // Addressed to one tab and one phone: a tab holding any other phone leaves it alone.
        if (message.target === this.tab && this.held !== null && this.held === message.serial) {
          this.releaseHandler?.();
        }
        break;
    }
  };

  // A closed or reloaded tab never gets to say it let go; do it on its way out.
  private readonly onPageHide = (): void => this.announceReleased();

  constructor() {
    this.channel?.addEventListener('message', this.onMessage);
    globalThis.addEventListener?.('pagehide', this.onPageHide);
    this.post({ type: 'query', tab: this.tab });
  }

  public ngOnDestroy(): void {
    globalThis.removeEventListener?.('pagehide', this.onPageHide);
    this.channel?.removeEventListener('message', this.onMessage);
    this.channel?.close();
  }

  /** The relay registers how this tab lets go of its phone when another tab asks. */
  public onReleaseRequest(handler: () => void): void {
    this.releaseHandler = handler;
  }

  public announceHeld(serial: string): void {
    this.held = serial;
    this.post({ type: 'held', tab: this.tab, serial });
  }

  public announceReleased(): void {
    if (this.held === null) return;
    this.held = null;
    this.post({ type: 'released', tab: this.tab });
  }

  /**
   * Ask the tab that holds `serial` to let go. Resolves when that tab did, or after a short wait
   * if it never answers. No tab holds it: resolves at once.
   */
  public requestRelease(serial: string | null | undefined): Promise<void> {
    const holder = serial ? [...this.holders].find(([, held]) => held === serial)?.[0] : undefined;
    if (holder === undefined || !serial) return Promise.resolve();
    return new Promise((resolve) => {
      const waiters = this.waiting.get(holder) ?? new Set<() => void>();
      this.waiting.set(holder, waiters);
      const done = (): void => {
        clearTimeout(timer);
        waiters.delete(done);
        resolve();
      };
      const timer = setTimeout(done, TAKE_OVER_TIMEOUT_MS);
      waiters.add(done);
      this.post({ type: 'release', tab: this.tab, target: holder, serial });
    });
  }

  private post(message: TabMessage): void {
    this.channel?.postMessage(message);
  }
}
