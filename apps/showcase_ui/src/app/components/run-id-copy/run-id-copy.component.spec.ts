import { Component } from '@angular/core';
import { By } from '@angular/platform-browser';
import { TestBed } from '@angular/core/testing';
import { RunIdCopyComponent } from './run-id-copy.component';

@Component({
  standalone: true,
  imports: [RunIdCopyComponent],
  template: '<app-run-id-copy [runId]="runId" />'
})
class RunIdCopyHostComponent {
  public runId = '01234567-89ab-cdef-0123-456789abcdef';
}

describe('RunIdCopyComponent', () => {
  it('shows eight characters and copies the full run id with confirmation', async () => {
    const writeText = jasmine.createSpy('writeText').and.resolveTo(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText }
    });

    await TestBed.configureTestingModule({ imports: [RunIdCopyHostComponent] }).compileComponents();
    const fixture = TestBed.createComponent(RunIdCopyHostComponent);
    fixture.detectChanges();

    const button = fixture.nativeElement.querySelector('button') as HTMLButtonElement;
    const component = fixture.debugElement.query(By.directive(RunIdCopyComponent)).componentInstance as RunIdCopyComponent;
    const copyRunId = spyOn(component, 'copyRunId').and.callThrough();
    expect(button.textContent?.trim()).toBe('ID: 01234567');
    button.click();
    await copyRunId.calls.mostRecent().returnValue;
    fixture.detectChanges();

    expect(writeText).toHaveBeenCalledOnceWith('01234567-89ab-cdef-0123-456789abcdef');
    expect(fixture.nativeElement.textContent).toContain('Copied');
  });
});
