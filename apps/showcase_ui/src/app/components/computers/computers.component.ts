import { Component, ChangeDetectionStrategy, DestroyRef, ElementRef, OnInit, computed, inject, signal, viewChild } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { HttpErrorResponse } from '@angular/common/http';
import { EMPTY, Subscription, catchError, switchMap, timer } from 'rxjs';
import { Computer, EnrollmentCode, HostsResponse } from '../../core/models/host.model';
import { AdminConfigService } from '../../services/admin-config.service';
import { HostsService } from '../../services/hosts.service';
import {
  COMPUTER_STRINGS,
  STATUS_LABEL,
  formatCountdown,
  installMessage,
  phonesSummary,
  reasonText,
  revokeWarning
} from '../../utils/computer-strings';
import { DeviceChipComponent } from '../device-chip/device-chip.component';

type DialogState = 'closed' | 'creating' | 'waiting' | 'connected' | 'expired' | 'failed';

@Component({
  selector: 'app-computers',
  standalone: true,
  imports: [DeviceChipComponent],
  templateUrl: './computers.component.html',
  styleUrl: './computers.component.scss',
  changeDetection: ChangeDetectionStrategy.Eager
})
export class ComputersComponent implements OnInit {
  private readonly hosts = inject(HostsService);
  private readonly adminConfig = inject(AdminConfigService);
  private readonly destroyRef = inject(DestroyRef);

  public readonly strings = COMPUTER_STRINGS;
  public readonly statusLabel = STATUS_LABEL;
  public readonly phonesSummary = phonesSummary;
  public readonly reasonText = reasonText;

  public readonly data = signal<HostsResponse | null>(null);
  public readonly loading = signal(true);
  public readonly failed = signal(false);
  public readonly isAdmin = signal(false);
  public readonly revoking = signal<Computer | null>(null);
  public readonly renaming = signal<string | null>(null);

  public readonly dialog = signal<DialogState>('closed');
  public readonly code = signal<EnrollmentCode | null>(null);
  public readonly connectedName = signal('');
  public readonly dialogError = signal('');
  public readonly copied = signal(false);
  public readonly nowMs = signal(Date.now());
  public readonly countdown = computed(() => {
    const code = this.code();
    return code ? formatCountdown(code.expires_at * 1000 - this.nowMs()) : '';
  });
  public readonly revokeText = computed(() => {
    const host = this.revoking();
    return host ? revokeWarning(host.name, host.active_run_count) : '';
  });

  private readonly dialogBox = viewChild<ElementRef<HTMLElement>>('dialogBox');
  private opener: HTMLElement | null = null;
  private watch: Subscription | null = null;

  public ngOnInit(): void {
    this.adminConfig
      .getIdentity()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({ next: (identity) => this.isAdmin.set(identity.admin), error: () => this.isAdmin.set(false) });
    this.load();
  }

  public load(): void {
    this.loading.set(true);
    this.failed.set(false);
    this.hosts
      .list()
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (response) => {
          this.data.set(response);
          this.loading.set(false);
        },
        error: () => {
          this.failed.set(true);
          this.loading.set(false);
        }
      });
  }

  public since(epochSeconds: number): string {
    const minutes = Math.max(0, Math.floor((Date.now() / 1000 - epochSeconds) / 60));
    if (minutes < 1) return 'just now';
    if (minutes < 60) return `${minutes} min ago`;
    const hours = Math.floor(minutes / 60);
    return hours < 24 ? `${hours} h ago` : `${Math.floor(hours / 24)} d ago`;
  }

  public shareCommand(host: Computer): string {
    return host.share_command.replace('<serial>', host.unshared_serials[0] ?? '<serial>');
  }

  // -- revoke --------------------------------------------------------------
  public askRevoke(host: Computer): void {
    this.revoking.set(host);
  }

  public cancelRevoke(): void {
    this.revoking.set(null);
  }

  public confirmRevoke(): void {
    const host = this.revoking();
    if (!host) return;
    this.hosts.revoke(host.id).subscribe({
      next: () => {
        this.revoking.set(null);
        this.load();
      },
      error: () => this.revoking.set(null)
    });
  }

  // -- rename --------------------------------------------------------------
  public startRename(host: Computer): void {
    this.renaming.set(host.id);
  }

  public saveRename(host: Computer, name: string): void {
    const trimmed = name.trim();
    this.renaming.set(null);
    if (trimmed && trimmed !== host.name) {
      this.hosts.rename(host.id, trimmed).subscribe({ next: () => this.load() });
    }
  }

  // -- connect a computer ----------------------------------------------------
  public openConnect(event: Event): void {
    this.opener = event.currentTarget as HTMLElement;
    this.code.set(null);
    this.copied.set(false);
    this.dialogError.set('');
    this.dialog.set('creating');
    setTimeout(() => this.dialogBox()?.nativeElement.focus());
    this.hosts.createCode().subscribe({
      next: (code) => {
        this.code.set(code);
        this.dialog.set('waiting');
        this.startWatching(code);
      },
      error: (error: HttpErrorResponse) => {
        this.dialog.set('failed');
        this.dialogError.set(this.createFailure(error));
      }
    });
  }

  public closeDialog(): void {
    this.watch?.unsubscribe();
    this.watch = null;
    this.dialog.set('closed');
    this.code.set(null);
    const opener = this.opener;
    setTimeout(() => opener?.focus());
  }

  public onDialogKey(event: KeyboardEvent): void {
    if (event.key === 'Escape') {
      event.stopPropagation();
      this.closeDialog();
    }
  }

  public async copyInstallMessage(): Promise<void> {
    const code = this.code();
    if (!code) return;
    await navigator.clipboard?.writeText(installMessage(window.location.origin, code.code, code.expires_at));
    this.copied.set(true);
  }

  private startWatching(code: EnrollmentCode): void {
    this.watch?.unsubscribe();
    this.watch = timer(0, 1000)
      .pipe(
        switchMap((tick) => {
          this.nowMs.set(Date.now());
          return tick % 2 === 1 ? this.hosts.codeStatus(code.code_id).pipe(catchError(() => EMPTY)) : EMPTY;
        }),
        takeUntilDestroyed(this.destroyRef)
      )
      .subscribe((status) => {
        if (status.status === 'connected') {
          this.connectedName.set(status.computer_name ?? 'The computer');
          this.code.set(null); // shown once
          this.dialog.set('connected');
          this.watch?.unsubscribe();
          this.load();
        } else if (status.status === 'expired') {
          this.dialog.set('expired');
          this.watch?.unsubscribe();
        }
      });
  }

  private createFailure(error: HttpErrorResponse): string {
    if (error.status === 429) return COMPUTER_STRINGS.rateLimited;
    if (error.status === 401 || error.status === 403) return COMPUTER_STRINGS.adminOnly;
    return COMPUTER_STRINGS.loadFailed;
  }
}
