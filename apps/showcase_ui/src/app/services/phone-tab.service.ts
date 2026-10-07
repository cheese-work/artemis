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
  | { type: 'release'; tab: string };

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
  private readonly released = new Set<() => void>();

  /** The phone another tab of this browser holds; null when none does. */
  public readonly heldElsewhere = signal<{ serial: string } | null>(null);
  private elsewhereTab: string | null = null;

  private readonly onMessage = (event: MessageEvent): void => {
    const message = event.data as TabMessage | null;
    if (!message || message.tab === this.tab) return;
    switch (message.type) {
      case 'query':
        if (this.held) this.post({ type: 'held', tab: this.tab, serial: this.held });
        break;
      case 'held':
        this.elsewhereTab = message.tab;
        this.heldElsewhere.set({ serial: message.serial });
        break;
      case 'released':
        if (this.elsewhereTab === message.tab) {
          this.elsewhereTab = null;
          this.heldElsewhere.set(null);
        }
        this.released.forEach((resolve) => resolve());
        break;
      case 'release':
        if (this.held) this.releaseHandler?.();
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

  /** Ask the holding tab to let go; resolves when it did, or after a short wait if it never answers. */
  public requestRelease(): Promise<void> {
    if (!this.heldElsewhere()) return Promise.resolve();
    return new Promise((resolve) => {
      const done = (): void => {
        clearTimeout(timer);
        this.released.delete(done);
        resolve();
      };
      const timer = setTimeout(done, TAKE_OVER_TIMEOUT_MS);
      this.released.add(done);
      this.post({ type: 'release', tab: this.tab });
    });
  }

  private post(message: TabMessage): void {
    this.channel?.postMessage(message);
  }
}
