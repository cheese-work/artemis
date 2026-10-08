import { HttpClient } from '@angular/common/http';
import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { DeployVersion, parseDeployVersion } from '../../core/models/deploy-version.model';

@Component({
  selector: 'app-version-footer',
  standalone: true,
  imports: [DatePipe],
  template: `
    <footer class="version-footer" aria-label="SmartQA version">
      @if (version(); as v) {
        SmartQA {{ v.shortSha }}@if (v.deployedAt) {, deployed <time [attr.datetime]="v.deployedAt">{{ v.deployedAt | date: 'yyyy-MM-dd HH:mm' }}</time>}
      } @else {
        SmartQA version unknown
      }
    </footer>
  `,
  styles: [`
    .version-footer {
      position: fixed;
      right: 10px;
      bottom: 4px;
      z-index: 1;
      color: #64748b;
      font-size: 11px;
      pointer-events: none;
    }
  `],
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class VersionFooterComponent {
  public readonly version = signal<DeployVersion | null>(null);

  constructor() {
    inject(HttpClient).get<unknown>('/api/system/version').subscribe({
      next: value => this.version.set(parseDeployVersion(value)),
      error: () => this.version.set(null)
    });
  }
}
