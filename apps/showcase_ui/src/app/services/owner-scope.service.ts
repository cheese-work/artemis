import { Injectable, computed, inject, signal } from '@angular/core';
import { AdminConfigService, AdminIdentity } from './admin-config.service';

/**
 * Whose runs this browser shows (CHE-1150). The server already limits every list
 * to the caller's own runs; an admin can widen it with the "All users" switch.
 * Open mode (local use, no sign-in) never filters, so the switch is not offered.
 */
@Injectable({ providedIn: 'root' })
export class OwnerScopeService {
  private readonly adminApi = inject(AdminConfigService);
  private asked = false;
  private readonly wantsAll = signal(false);

  public readonly identity = signal<AdminIdentity | null>(null);
  /** True for an admin signed in through Cloudflare; the only caller the switch is for. */
  public readonly canSwitch = computed(() => {
    const who = this.identity();
    return who?.admin === true && who.auth_mode !== 'open';
  });
  /** Off until an admin turns it on. */
  public readonly allUsers = computed(() => this.wantsAll() && this.canSwitch());

  /** Reads who the caller is, once for the whole page. */
  public load(): void {
    if (this.asked) return;
    this.asked = true;
    this.adminApi.getIdentity().subscribe({
      next: (who) => this.identity.set(who),
      error: () => this.identity.set(null)
    });
  }

  public setAllUsers(on: boolean): void {
    this.wantsAll.set(on);
  }

  /** Whether the server lets the caller stop, resume or delete a run with this owner. */
  public canManage(owner: string | null | undefined): boolean {
    const who = this.identity();
    if (!who) return false;
    if (who.auth_mode === 'open' || who.admin) return true;
    return !!owner && owner === who.email;
  }
}
