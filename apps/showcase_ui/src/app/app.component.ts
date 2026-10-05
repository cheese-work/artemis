/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

import { Component, ChangeDetectionStrategy, inject, ViewChild } from '@angular/core';
import { RouterOutlet } from '@angular/router';
import { NavSwitcherComponent } from './components/nav-switcher/nav-switcher.component';
import { WhatsNewComponent } from './components/whats-new/whats-new.component';
import { VersionFooterComponent } from './components/version-footer/version-footer.component';
import { AgentService } from './services/agent.service';

@Component({
  selector: 'app-root',
  imports: [RouterOutlet, NavSwitcherComponent, WhatsNewComponent, VersionFooterComponent],
  templateUrl: './app.component.html',
  changeDetection: ChangeDetectionStrategy.Eager,
  styleUrl: './app.component.scss'
})
export class AppComponent {
  public title = 'SmartQA';
  public readonly agentService = inject(AgentService);

  @ViewChild(WhatsNewComponent) private whatsNew?: WhatsNewComponent;

  public openWhatsNew(): void {
    this.whatsNew?.open();
  }
}
