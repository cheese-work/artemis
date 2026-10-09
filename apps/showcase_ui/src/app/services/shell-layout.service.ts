import { Injectable, signal } from '@angular/core';

@Injectable({ providedIn: 'root' })
export class ShellLayoutService {
  public readonly activePane = signal<'list' | 'detail'>('list');
}
