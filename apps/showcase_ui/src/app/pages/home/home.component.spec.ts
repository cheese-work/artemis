import { signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { Router } from '@angular/router';

import { TaskRecommendationService } from '../../core/services/task-recommendation.service';
import { AgentService } from '../../services/agent.service';
import { SystemService } from '../../services/system.service';
import { HomeComponent } from './home.component';

describe('HomeComponent credential inputs', () => {
  let fixture: ComponentFixture<HomeComponent>;

  beforeEach(async () => {
    const fakeGeminiSecret = 'FAKE-GEMINI-SECRET-NOT-PREFILLED';
    const fakeOcrSecret = 'FAKE-OCR-SECRET-NOT-PREFILLED';
    const systemService = {
      configWritesLocked: signal(true),
      llmProbe: () => ({
        metadata: {
          providers: [{ provider: 'google', is_set: true, masked: '****1234' }],
          current_key: fakeGeminiSecret,
          api_keys: { google: fakeGeminiSecret }
        }
      }),
      ocrProbe: () => ({
        metadata: {
          is_set: true,
          masked: '****5678',
          key: fakeOcrSecret,
          raw_key: fakeOcrSecret
        }
      }),
      currentApiKey: () => fakeGeminiSecret,
      apiKeysMap: () => ({ google: fakeGeminiSecret, ocr: fakeOcrSecret }),
      fetchReadiness: () => of({}),
      fetchCredentialStatus: () => of({ config_writes_locked: true }),
      fetchModelConfigEnv: () => of({}),
      fetchAdbServerStatus: () => of({ endpoint: { mode: 'local' } })
    };

    await TestBed.configureTestingModule({
      imports: [HomeComponent],
      providers: [
        { provide: AgentService, useValue: { getProTuningDefaults: () => of({}) } },
        { provide: SystemService, useValue: systemService },
        { provide: TaskRecommendationService, useValue: { allTasks: [] } },
        { provide: Router, useValue: {} }
      ]
    })
      .overrideComponent(HomeComponent, {
        set: {
          template: '<input name="gemini" [ngModel]="geminiKeyInput()" [disabled]="configWritesLocked()"><input name="ocr" [ngModel]="ocrKeyInput()" [disabled]="configWritesLocked()">'
        }
      })
      .compileComponents();

    fixture = TestBed.createComponent(HomeComponent);
    fixture.detectChanges();
  });

  afterEach(() => fixture.destroy());

  it('keeps configured Gemini and OCR inputs blank instead of pre-filling keys', () => {
    const component = fixture.componentInstance;
    const geminiInput = fixture.nativeElement.querySelector('[name="gemini"]') as HTMLInputElement;
    const ocrInput = fixture.nativeElement.querySelector('[name="ocr"]') as HTMLInputElement;

    expect(component.isGeminiConfigured()).toBeTrue();
    expect(component.isOcrConfigured()).toBeTrue();
    expect(component.configWritesLocked()).toBeTrue();
    expect(component.geminiKeyInput()).toBe('');
    expect(component.ocrKeyInput()).toBe('');
    expect(geminiInput.value).toBe('');
    expect(ocrInput.value).toBe('');
    expect(geminiInput.disabled).toBeTrue();
    expect(ocrInput.disabled).toBeTrue();
  });
});
