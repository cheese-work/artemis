import { Component } from '@angular/core';
import { By } from '@angular/platform-browser';
import { TestBed } from '@angular/core/testing';
import { RunIdCopyComponent } from './run-id-copy.component';

@Component({
  standalone: true,
  imports: [RunIdCopyComponent],
  template: `
    @if (menuOpen) {
      <div class="vscode-dropdown-menu task-dropdown">
        <div class="dropdown-item-card" (click)="selectTask()">
          <app-run-id-copy [runId]="runId" />
        </div>
      </div>
    }
  `
})
class RunIdCopyHostComponent {
  public runId = '01234567-89ab-cdef-0123-456789abcdef';
  public menuOpen = true;
  public selected = false;

  public selectTask(): void {
    this.selected = true;
    this.menuOpen = false;
  }
}

describe('RunIdCopyComponent', () => {
  const originalClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard');

  afterEach(() => {
    if (originalClipboard) {
      Object.defineProperty(navigator, 'clipboard', originalClipboard);
    } else {
      delete (navigator as { clipboard?: Clipboard }).clipboard;
    }
  });

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
    expect(fixture.componentInstance.menuOpen).toBeTrue();
    expect(fixture.componentInstance.selected).toBeFalse();
  });

  it('selects the full ID and gives a manual copy instruction when clipboard access is denied', async () => {
    const writeText = jasmine.createSpy('writeText').and.rejectWith(new DOMException('Denied', 'NotAllowedError'));
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText }
    });

    await TestBed.configureTestingModule({ imports: [RunIdCopyHostComponent] }).compileComponents();
    const fixture = TestBed.createComponent(RunIdCopyHostComponent);
    fixture.detectChanges();
    const component = fixture.debugElement.query(By.directive(RunIdCopyComponent)).componentInstance as RunIdCopyComponent;
    const copyRunId = spyOn(component, 'copyRunId').and.callThrough();
    (fixture.nativeElement.querySelector('button') as HTMLButtonElement).click();
    await copyRunId.calls.mostRecent().returnValue;
    fixture.detectChanges();
    await new Promise(resolve => setTimeout(resolve, 0));
    fixture.detectChanges();

    const fallback = fixture.nativeElement.querySelector('.copy-fallback input') as HTMLInputElement;
    expect(writeText).toHaveBeenCalledOnceWith('01234567-89ab-cdef-0123-456789abcdef');
    expect(fallback.value).toBe('01234567-89ab-cdef-0123-456789abcdef');
    expect(fallback.selectionStart).toBe(0);
    expect(fallback.selectionEnd).toBe(fallback.value.length);
    expect(fixture.nativeElement.textContent).toContain('Press Ctrl+C');
  });

  it('keeps compact-menu ID copy confirmation visible without selecting the run', async () => {
    const writeText = jasmine.createSpy('writeText').and.resolveTo(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText }
    });

    await TestBed.configureTestingModule({ imports: [RunIdCopyHostComponent] }).compileComponents();
    const fixture = TestBed.createComponent(RunIdCopyHostComponent);
    fixture.detectChanges();
    const component = fixture.debugElement.query(By.directive(RunIdCopyComponent)).componentInstance as RunIdCopyComponent;
    const copyRunId = spyOn(component, 'copyRunId').and.callThrough();
    (fixture.nativeElement.querySelector('.task-dropdown button') as HTMLButtonElement).click();
    await copyRunId.calls.mostRecent().returnValue;
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('.task-dropdown')).not.toBeNull();
    expect(fixture.nativeElement.textContent).toContain('Copied');
    expect(fixture.componentInstance.selected).toBeFalse();
  });

  it('keeps the compact menu and manual-copy field open when the field is clicked', async () => {
    const writeText = jasmine.createSpy('writeText').and.rejectWith(new DOMException('Denied', 'NotAllowedError'));
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText }
    });

    await TestBed.configureTestingModule({ imports: [RunIdCopyHostComponent] }).compileComponents();
    const fixture = TestBed.createComponent(RunIdCopyHostComponent);
    fixture.detectChanges();
    const component = fixture.debugElement.query(By.directive(RunIdCopyComponent)).componentInstance as RunIdCopyComponent;
    const copyRunId = spyOn(component, 'copyRunId').and.callThrough();
    (fixture.nativeElement.querySelector('.task-dropdown button') as HTMLButtonElement).click();
    await copyRunId.calls.mostRecent().returnValue;
    fixture.detectChanges();
    await new Promise(resolve => setTimeout(resolve, 0));
    fixture.detectChanges();

    const fallback = fixture.nativeElement.querySelector('.copy-fallback input') as HTMLInputElement;
    fallback.click();
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('.task-dropdown')).not.toBeNull();
    expect(fallback.isConnected).toBeTrue();
    expect(fixture.componentInstance.menuOpen).toBeTrue();
    expect(fixture.componentInstance.selected).toBeFalse();
  });
});
