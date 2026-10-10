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

import { TestBed } from '@angular/core/testing';
import { RouterOutlet, provideRouter } from '@angular/router';
import { provideHttpClient, withXhr } from '@angular/common/http';
import { HttpClient } from '@angular/common/http';
import { Component, EventEmitter, Input, Output, signal } from '@angular/core';
import { of } from 'rxjs';
import { AgentService } from './services/agent.service';
import { AppComponent } from './app.component';
import { VersionFooterComponent } from './components/version-footer/version-footer.component';

describe('AppComponent', () => {
  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [AppComponent],
      providers: [
        provideRouter([]),
        provideHttpClient(withXhr()),
        { provide: HttpClient, useValue: { get: () => of([]) } },
        {
          provide: AgentService,
          useValue: {
            agentStatus: signal('idle'),
            activeTasks: signal([]),
            hasFetchedStatus: signal(true)
          }
        }
      ]
    }).compileComponents();
  });

  it('should create the app', () => {
    const fixture = TestBed.createComponent(AppComponent);
    const app = fixture.componentInstance;
    expect(app).toBeTruthy();
  });

  it(`should have the 'SmartQA' title`, () => {
    const fixture = TestBed.createComponent(AppComponent);
    const app = fixture.componentInstance;
    expect(app.title).toEqual('SmartQA');
  });

});

describe('AppComponent layout', () => {
  @Component({ selector: 'app-nav-switcher', template: '' })
  class NavStub {
    @Input() hasWhatsNew = false;
    @Input() hasUnreadWhatsNew = false;
    @Output() showWhatsNew = new EventEmitter<void>();
  }

  @Component({ selector: 'app-whats-new', template: '' })
  class WhatsNewStub {}

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [AppComponent],
      providers: [
        provideRouter([]),
        { provide: HttpClient, useValue: { get: () => of([]) } },
        { provide: AgentService, useValue: { whatsNewHasUpdates: signal(false), whatsNewHasUnread: signal(false) } }
      ]
    })
      .overrideComponent(AppComponent, { set: { imports: [RouterOutlet, NavStub, WhatsNewStub, VersionFooterComponent] } })
      .compileComponents();
  });

  it('hides the legacy footer on desktop and reserves its strip below the scrolling page on mobile', () => {
    const fixture = TestBed.createComponent(AppComponent);
    fixture.detectChanges();
    const root = fixture.nativeElement as HTMLElement;
    const page = root.querySelector<HTMLElement>('.app-page');
    const footer = root.querySelector<HTMLElement>('app-version-footer');

    expect(page).withContext('page area').not.toBeNull();
    expect(page!.contains(footer)).toBeFalse();
    expect(getComputedStyle(page!).overflowY).toBe('auto');
    if (window.innerWidth >= 1024) {
      expect(getComputedStyle(footer!).display).toBe('none');
      expect(page!.getBoundingClientRect().left).toBe(224);
    } else {
      expect(getComputedStyle(footer!.querySelector('footer')!).position).not.toBe('fixed');
      expect(footer!.getBoundingClientRect().top).toBeGreaterThanOrEqual(page!.getBoundingClientRect().bottom);
    }
  });
});
