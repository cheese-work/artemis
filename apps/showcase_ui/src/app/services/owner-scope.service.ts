import { Injectable, computed, inject, signal } from '@angular/core';
import { AdminConfigService, AdminIdentity } from './admin-config.service';

/** Shown for a run submitted without an identity (CLI or SDK on the server). */
const NO_OWNER = 'No owner';

/**
 * Whose runs this browser shows. A QA sees only their own; an admin can switch
 * "All users" on (off by default). The server enforces it: this only decides
 * what the page asks for and which controls it offers. Open mode (local or CLI)
 * has no identity to filter by, so nothing changes there.
 */
@Injectable({ providedIn: 'root' })
export class OwnerScopeService {
  private readonly adminApi = inject(AdminConfigService);
  private requested = false;

  /** Null until the first answer arrives. */
  public readonly identity = signal<AdminIdentity | null>(null);
  /** The admin's switch. Honoured only for an admin in a filtered mode: see `showAll`. */
  public readonly allUsers = signal(false);

  public readonly canSeeAll = computed(() => {
    const who = this.identity();
    return !!who && who.auth_mode !== 'open' && who.admin;
  });
  public readonly showAll = computed(() => this.canSeeAll() && this.allUsers());

  /** Idempotent: every component that needs the identity calls it. */
  public load(): void {
    if (this.requested) return;
    this.requested = true;
    this.adminApi.getIdentity().subscribe({
      next: (who) => this.identity.set(who),
      // A failed lookup must never unlock changes the server would refuse.
      error: () => this.identity.set({ email: null, admin: false, auth_mode: 'cloudflare', reason: 'whoami_failed' })
    });
  }

  public setAllUsers(on: boolean): void {
    this.allUsers.set(on);
  }

  /** The `scope` query for list, queue and stream requests; empty means "mine". */
  public queryParams(): Record<string, string> {
    return this.showAll() ? { scope: 'all' } : {};
  }

  /** Mirrors the server: the owner, an admin, or anyone in open mode may change a run. */
  public canAct(owner: string | null | undefined): boolean {
    const who = this.identity();
    if (!who) return false;
    return who.auth_mode === 'open' || who.admin || (!!owner && owner === who.email);
  }

  public ownerText(owner: string | null | undefined): string {
    return owner || NO_OWNER;
  }
}
