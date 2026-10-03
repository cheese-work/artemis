import { Component, ChangeDetectionStrategy, Input, OnInit, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { finalize, forkJoin } from 'rxjs';
import { AdminConfigService, AdminIdentity, ConfigSnapshot, ModelSelection } from '../../services/admin-config.service';

@Component({
  selector: 'app-setup',
  standalone: true,
  imports: [FormsModule],
  templateUrl: './setup.component.html',
  styleUrl: './setup.component.scss',
  host: { '[class.embedded]': 'embedded' },
  changeDetection: ChangeDetectionStrategy.Eager
})
export class SetupComponent implements OnInit {
  @Input() public embedded = false;
  private readonly adminConfig = inject(AdminConfigService);
  public readonly identity = signal<AdminIdentity | null>(null);
  public readonly snapshot = signal<ConfigSnapshot | null>(null);
  public readonly loading = signal(true);
  public readonly saving = signal(false);
  public readonly message = signal('');
  public readonly conflict = signal(false);
  public readonly modelProviders = computed(
    () => (this.snapshot()?.providers ?? []).filter((provider) => provider.name !== 'ocr')
  );
  public primaryProvider = '';
  public primaryModel = '';
  public fallbackProvider = '';
  public fallbackModel = '';
  public credentialInputs: Record<string, string> = {};
  public baseUrls: Record<string, string> = {};
  private originalBaseUrls: Record<string, string> = {};
  private readonly credentialsToClear = new Set<string>();

  public ngOnInit(): void {
    this.load();
  }

  public canEdit(): boolean {
    return this.identity()?.admin === true;
  }

  public load(): void {
    this.loading.set(true);
    this.snapshot.set(null);
    this.message.set('');
    this.conflict.set(false);
    forkJoin({
      identity: this.adminConfig.getIdentity(),
      config: this.adminConfig.getConfig()
    }).pipe(finalize(() => this.loading.set(false))).subscribe({
      next: ({ identity, config }) => {
        this.identity.set(identity);
        this.snapshot.set(config);
        this.primaryProvider = config.default.provider;
        this.primaryModel = config.default.model;
        this.fallbackProvider = config.default.fallback?.provider ?? config.default.provider;
        this.fallbackModel = config.default.fallback?.model ?? config.default.model;
        this.baseUrls = Object.fromEntries(
          config.providers.map((provider) => [provider.name, provider.base_url ?? ''])
        );
        this.originalBaseUrls = { ...this.baseUrls };
        this.credentialInputs = {};
        this.credentialsToClear.clear();
      },
      error: (error) => {
        this.message.set(this.errorMessage(error));
      }
    });
  }

  public clearCredential(provider: string): void {
    this.credentialInputs[provider] = '';
    this.credentialsToClear.add(provider);
  }

  public onCredentialInput(provider: string, value: string): void {
    this.credentialInputs[provider] = value;
    if (value.trim()) {
      this.credentialsToClear.delete(provider);
    }
  }

  public save(): void {
    const current = this.snapshot();
    if (!current || !this.canEdit() || this.saving()) {
      return;
    }

    const fallback: ModelSelection = {
      ...(current.default.fallback ?? { provider: this.fallbackProvider, model: this.fallbackModel }),
      provider: this.fallbackProvider,
      model: this.fallbackModel
    };
    const defaultConfig = {
      ...current.default,
      provider: this.primaryProvider,
      model: this.primaryModel,
      fallback
    };
    const credentials: Record<string, string | null> = {};
    for (const provider of current.providers) {
      if (this.credentialInputs[provider.name]?.trim()) {
        credentials[provider.name] = this.credentialInputs[provider.name].trim();
      } else if (this.credentialsToClear.has(provider.name)) {
        credentials[provider.name] = null;
      }
    }
    const baseUrls = Object.fromEntries(
      ['openai', 'anthropic']
        .filter((provider) => this.baseUrls[provider] !== this.originalBaseUrls[provider])
        .map((provider) => [provider, this.baseUrls[provider] || null])
    );

    this.saving.set(true);
    this.message.set('');
    this.conflict.set(false);
    this.adminConfig.saveConfig({
      version: current.version,
      default: defaultConfig,
      credentials,
      base_urls: baseUrls
    }).pipe(finalize(() => this.saving.set(false))).subscribe({
      next: (response) => {
        this.snapshot.set(response.config);
        this.primaryProvider = response.config.default.provider;
        this.primaryModel = response.config.default.model;
        this.fallbackProvider = response.config.default.fallback?.provider ?? this.primaryProvider;
        this.fallbackModel = response.config.default.fallback?.model ?? this.primaryModel;
        this.credentialInputs = {};
        this.credentialsToClear.clear();
        this.originalBaseUrls = { ...this.baseUrls };
        this.message.set('Saved. Changes apply to your next run.');
      },
      error: (error) => {
        this.conflict.set(error?.status === 409 && error?.error?.code === 'config_conflict');
        this.message.set(this.errorMessage(error));
      }
    });
  }

  private errorMessage(error: any): string {
    if (error?.status === 401) {
      return 'Sign in through Cloudflare Access before saving settings.';
    }
    if (error?.status === 403) {
      return 'Admin only. Sign in with an allowlisted Cloudflare Access account to save settings.';
    }
    const code = error?.error?.code;
    if (code === 'config_source_conflict' || code === 'config_unwritable') {
      return [error?.error?.detail, error?.error?.fix].filter(Boolean).join(' ');
    }
    if (code === 'config_conflict' || error?.status === 409) {
      return 'Settings changed elsewhere. Reload to review.';
    }
    return typeof error?.error?.detail === 'string'
      ? error.error.detail
      : 'Settings could not be loaded. Check the server and try again.';
  }
}
