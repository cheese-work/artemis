import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { of, throwError } from 'rxjs';
import { FailureReport } from '../../core/models/failure.model';
import { FailuresService } from '../../services/failures.service';
import { FailuresComponent } from './failures.component';

const report: FailureReport = {
  days: 14,
  counts: [
    { day: '2026-10-07', category: 'smartqa_agent', count: 3 },
    { day: '2026-10-07', category: 'user_prompt', count: 1 }
  ],
  causes: [
    {
      category: 'smartqa_agent',
      rule: 'empty_target_list',
      count: 3,
      sample: 'Invalid target index 2. The list is empty on this screen.',
      session_ids: ['22d8d458-ba2a-4417-8261-cf8d0d1117d5'],
      action: 'fix'
    },
    {
      category: 'user_prompt',
      rule: 'app_not_installed',
      count: 1,
      sample: 'Error finding package for app: ClimaMap',
      session_ids: ['c5d16609-e38d-4a70-9cb3-5f481837f0bb'],
      action: 'no action'
    }
  ]
};

describe('FailuresComponent', () => {
  let service: jasmine.SpyObj<FailuresService>;
  let fixture: ComponentFixture<FailuresComponent>;

  beforeEach(async () => {
    service = jasmine.createSpyObj<FailuresService>('FailuresService', ['report']);
    await TestBed.configureTestingModule({
      imports: [FailuresComponent],
      providers: [provideRouter([]), { provide: FailuresService, useValue: service }]
    }).compileComponents();
  });

  function render(): HTMLElement {
    fixture = TestBed.createComponent(FailuresComponent);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  it('counts failures by day and cause and links each cause to its runs', () => {
    service.report.and.returnValue(of(report));
    const root = render();
    const rows = Array.from(root.querySelectorAll('.counts tbody tr')).map((row) =>
      Array.from(row.querySelectorAll('td')).map((cell) => cell.textContent)
    );
    expect(rows).toEqual([
      ['2026-10-07', 'SmartQA · agent', '3'],
      ['2026-10-07', 'Prompt', '1']
    ]);
    expect(root.querySelector('.cause a')?.getAttribute('href')).toBe('/runs/22d8d458-ba2a-4417-8261-cf8d0d1117d5');
  });

  it('marks prompt-side causes as no action', () => {
    service.report.and.returnValue(of(report));
    const causes = Array.from(render().querySelectorAll('.cause'));
    expect(causes[0].querySelector('.badge')?.textContent?.trim()).toBe('3×');
    expect(causes[1].classList).toContain('no-action');
    expect(causes[1].querySelector('.badge')?.textContent?.trim()).toBe('No action');
  });

  it('says so when nothing failed', () => {
    service.report.and.returnValue(of({ days: 14, counts: [], causes: [] }));
    expect(render().textContent).toContain('No failures in this window.');
  });

  it('offers a retry when the report cannot be read', () => {
    service.report.and.returnValues(throwError(() => new Error('down')), of(report));
    const root = render();
    expect(root.querySelector('[role="alert"]')).not.toBeNull();
    (root.querySelector('.retry') as HTMLButtonElement).click();
    fixture.detectChanges();
    expect(root.querySelector('.causes')).not.toBeNull();
  });
});
