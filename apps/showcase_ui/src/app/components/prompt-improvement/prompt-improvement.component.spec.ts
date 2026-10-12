import { TestBed } from '@angular/core/testing';
import { provideRouter, Router } from '@angular/router';
import { Subject, of, throwError } from 'rxjs';
import { PromptImprovementComponent } from './prompt-improvement.component';
import { PromptCritiquesService, PromptCritique } from '../../services/prompt-critiques.service';
import { ModelCatalogService } from '../../services/model-catalog.service';

const critique: PromptCritique = { id: 'c1', session_id: 'run1', created_at: 1, model: 'openai/default', original_prompt: 'Settings', improved_prompt: 'Open Settings and verify email.', changes: [{ what: 'Add a check', why: 'An observable result was missing' }], lessons: ['Name the expected result.'] };

describe('PromptImprovementComponent', () => {
  let api: { newest: jasmine.Spy; improve: jasmine.Spy };
  beforeEach(() => {
    api = { newest: jasmine.createSpy().and.returnValue(throwError(() => ({ status: 404 }))), improve: jasmine.createSpy().and.returnValue(of(critique)) };
    TestBed.configureTestingModule({ imports: [PromptImprovementComponent], providers: [provideRouter([]),
      { provide: PromptCritiquesService, useValue: api },
      { provide: ModelCatalogService, useValue: { load: () => undefined, options: () => [{ key: '', label: 'Server default' }], selectedKey: () => '', warning: () => null, choose: jasmine.createSpy() } }
    ] });
  });
  function open() {
    const fixture = TestBed.createComponent(PromptImprovementComponent);
    fixture.componentRef.setInput('sessionId', 'run1');
    fixture.componentRef.setInput('canImprove', true);
    fixture.detectChanges();
    return fixture;
  }
  it('only reads on load and shows idle, pending and result states', () => {
    const pending = new Subject<PromptCritique>();
    api.improve.and.returnValue(pending);
    const fixture = open();
    expect(api.newest).toHaveBeenCalledOnceWith('run1');
    expect(api.improve).not.toHaveBeenCalled();
    const button = fixture.nativeElement.querySelector('[data-improve]') as HTMLButtonElement;
    button.click(); fixture.detectChanges();
    expect(button.disabled).toBeTrue();
    expect(button.textContent).toContain('Improving…');
    pending.next(critique); pending.complete(); fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('What changed and why');
    expect(button.textContent).toContain('Improve again');
  });
  it('restores the newest critique, allows editing and hands off once', () => {
    api.newest.and.returnValue(of(critique));
    const fixture = open();
    const router = TestBed.inject(Router);
    spyOn(router, 'navigate').and.resolveTo(true);
    const textarea = fixture.nativeElement.querySelector('textarea') as HTMLTextAreaElement;
    expect(textarea.value).toBe(critique.improved_prompt);
    textarea.value = 'Edited prompt'; textarea.dispatchEvent(new Event('input')); fixture.detectChanges();
    fixture.nativeElement.querySelector('[data-run-improved]').click();
    expect(router.navigate).toHaveBeenCalledOnceWith(['/workspace'], { state: { draftPrompt: 'Edited prompt', autoStart: true } });
    expect(api.improve).not.toHaveBeenCalled();
  });
  it('shows generation errors with retry', () => {
    api.improve.and.returnValue(throwError(() => ({ error: { detail: 'openai/default: unavailable' } })));
    const fixture = open();
    fixture.nativeElement.querySelector('[data-improve]').click(); fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('[role="alert"]').textContent).toContain('openai/default');
    api.improve.and.returnValue(of(critique));
    fixture.nativeElement.querySelector('[data-improve]').click(); fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('textarea')).not.toBeNull();
  });
  it('keeps load failures distinct from no critique and offers a read retry', () => {
    api.newest.and.returnValue(throwError(() => ({ status: 500 })));
    const fixture = open();
    expect(fixture.nativeElement.textContent).toContain('Could not load');
    api.newest.and.returnValue(of(critique));
    fixture.nativeElement.querySelector('[data-retry-load]').click(); fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('textarea')).not.toBeNull();
    expect(api.improve).not.toHaveBeenCalled();
  });
  it('hides generation from shared readers and ignores an old run response', () => {
    const old = new Subject<PromptCritique>(); api.newest.and.returnValue(old);
    const fixture = open();
    fixture.componentRef.setInput('canImprove', false);
    fixture.componentRef.setInput('sessionId', 'run2');
    api.newest.and.returnValue(throwError(() => ({ status: 404 })));
    fixture.detectChanges(); old.next(critique); fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('[data-improve]')).toBeNull();
    expect(fixture.nativeElement.querySelector('textarea')).toBeNull();
  });
});
