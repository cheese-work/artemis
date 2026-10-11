import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { Subject, of, throwError } from 'rxjs';
import { LessonsComponent } from './lessons.component';
import { PromptCritiquesService } from '../../services/prompt-critiques.service';
import { OwnerScopeService } from '../../services/owner-scope.service';

describe('LessonsComponent', () => {
  let api: { list: jasmine.Spy };
  beforeEach(() => {
    api = { list: jasmine.createSpy().and.returnValue(of({ critiques: [], next_offset: null })) };
    TestBed.configureTestingModule({ imports: [LessonsComponent], providers: [provideRouter([]),
      { provide: PromptCritiquesService, useValue: api },
      { provide: OwnerScopeService, useValue: { load: () => undefined, showAll: signal(false), canSeeAll: () => false } }
    ] });
  });
  it('shows loading, then an actionable empty state', () => {
    const response = new Subject<any>(); api.list.and.returnValue(response);
    const fixture = TestBed.createComponent(LessonsComponent); fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('Loading');
    response.next({ critiques: [], next_offset: null }); fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('Improve prompt');
  });
  it('shows errors and retries', () => {
    api.list.and.returnValue(throwError(() => new Error()));
    const fixture = TestBed.createComponent(LessonsComponent); fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('[role="alert"]')).not.toBeNull();
    api.list.and.returnValue(of({ critiques: [], next_offset: null }));
    fixture.nativeElement.querySelector('[data-retry]').click(); fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('[role="alert"]')).toBeNull();
  });
  it('searches with a visible label and links results to their run', () => {
    const fixture = TestBed.createComponent(LessonsComponent); fixture.detectChanges();
    const input = fixture.nativeElement.querySelector('input') as HTMLInputElement;
    expect(fixture.nativeElement.querySelector(`label[for="${input.id}"]`)).not.toBeNull();
    api.list.and.returnValue(of({ critiques: [{ id: 'c', session_id: 'run1', created_at: 1, model: 'openai/default', original_prompt: 'Settings', changes: [{ what: 'Name screen', why: 'Ambiguous' }], lessons: ['Specify outcome'] }], next_offset: null }));
    input.value = 'Settings'; input.dispatchEvent(new Event('input'));
    fixture.nativeElement.querySelector('form').dispatchEvent(new Event('submit')); fixture.detectChanges();
    expect(api.list).toHaveBeenCalledWith('Settings', 'mine', 0);
    expect(fixture.nativeElement.querySelector('a[href="/runs/run1"]')).not.toBeNull();
    expect(fixture.nativeElement.textContent).toContain('Ambiguous');
  });
});
