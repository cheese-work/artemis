import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { Observable } from 'rxjs';
import { HttpErrorResponse } from '@angular/common/http';
import { ProviderSettingsService, ProviderSlot, ProviderSnapshot } from '../../services/provider-settings.service';
import { ModelCatalogService } from '../../services/model-catalog.service';

type EditableSlot = ProviderSlot & { key: string; preset: string; original_url: string; original_enabled: boolean; clear_key: boolean };

@Component({
  standalone: true,
  selector: 'app-providers',
  imports: [FormsModule, RouterLink],
  templateUrl: './providers.component.html',
  styleUrl: './providers.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class ProvidersComponent {
  private readonly service = inject(ProviderSettingsService);
  private readonly catalog = inject(ModelCatalogService);
  private version = '';
  public readonly slots = signal<EditableSlot[]>([]);
  public readonly loading = signal(true);
  public readonly busy = signal<string | null>(null);
  public readonly error = signal<string | null>(null);
  public readonly notice = signal<string | null>(null);

  constructor() { this.reload(); }

  public reload(): void {
    this.loading.set(true);
    this.request(this.service.read());
  }

  public preset(slot: EditableSlot, name: string): void {
    slot.preset = name;
    const preset = slot.presets.find(preset => preset.name === name);
    if (preset) slot.base_url = preset.url;
  }

  public dirty(slot: EditableSlot): boolean {
    return !!slot.key || slot.clear_key || slot.base_url !== slot.original_url || slot.enabled !== slot.original_enabled;
  }

  public save(slot: EditableSlot): void {
    this.busy.set(slot.provider);
    this.request(this.service.save(slot.provider, {
      version: this.version, base_url: slot.base_url, enabled: slot.enabled,
      ...(slot.clear_key ? { key: '' } : slot.key ? { key: slot.key } : {})
    }), `${slot.label} saved. Changes apply to the next run.`);
    slot.key = '';
  }

  public action(slot: EditableSlot, action: 'test' | 'sync'): void {
    this.busy.set(slot.provider);
    this.request(this.service.action(slot.provider, action), action === 'sync' ? `${slot.label} model sync finished.` : null);
  }

  public time(value: number | null): string {
    return value === null ? 'Never' : new Date(value * 1000).toLocaleString('en-GB', { timeZone: 'Asia/Bangkok', day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false }) + ' ICT';
  }

  private request(result: Observable<ProviderSnapshot>, notice: string | null = null): void {
    this.error.set(null);
    this.notice.set(null);
    result.subscribe({
      next: snapshot => {
        this.version = snapshot.version;
        if (this.busy()) this.catalog.invalidate();
        const previous = this.slots();
        const changed = this.busy();
        this.slots.set(snapshot.slots.map(slot => {
          const editing = previous.find(candidate => candidate.provider === slot.provider);
          if (editing && slot.provider !== changed && this.dirty(editing)) return { ...slot, key: editing.key, base_url: editing.base_url, enabled: editing.enabled, preset: editing.preset, clear_key: editing.clear_key, original_url: slot.base_url, original_enabled: slot.enabled };
          return { ...slot, key: '', clear_key: false, original_url: slot.base_url, original_enabled: slot.enabled, preset: slot.presets.find(preset => preset.url === slot.base_url)?.name ?? 'Custom' };
        }));
        this.loading.set(false);
        this.busy.set(null);
        this.notice.set(notice);
      },
      error: (error: HttpErrorResponse) => {
        this.loading.set(false);
        this.busy.set(null);
        this.error.set(error.status === 403 ? 'Admin access required.' : error.error?.detail ?? 'Provider settings could not be updated. Reload and retry.');
      }
    });
  }
}
