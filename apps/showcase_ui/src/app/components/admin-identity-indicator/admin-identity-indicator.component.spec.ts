import { TestBed } from '@angular/core/testing';
import { HttpClient } from '@angular/common/http';
import { By } from '@angular/platform-browser';
import { provideRouter } from '@angular/router';
import { of, throwError } from 'rxjs';
import { AdminConfigService } from '../../services/admin-config.service';
import { AdminIdentityIndicatorComponent } from './admin-identity-indicator.component';
import { PAGE_BUILD, VersionFooterComponent } from '../version-footer/version-footer.component';
import { expectHitBox } from '../../testing/hit-box';

describe('AdminIdentityIndicatorComponent', () => {
  function create(admin: boolean, failed = false) {
    TestBed.configureTestingModule({
      imports: [AdminIdentityIndicatorComponent],
      providers: [provideRouter([]),
        { provide: HttpClient, useValue: { get: () => of({ status: 'known', sha: 'b'.repeat(40), short_sha: 'bbbbbbb', deployed_at: '2026-10-09T01:38:04Z' }) } },
        { provide: PAGE_BUILD, useValue: { sha: 'a'.repeat(40), builtAt: '2026-10-09T01:38:04Z' } }, {
        provide: AdminConfigService,
        useValue: {
          getIdentity: () => failed ? throwError(() => new Error('Unavailable')) : of({
            email: 'person@example.test', admin, auth_mode: 'cloudflare', reason: null
          })
        }
      }]
    });
    const fixture = TestBed.createComponent(AdminIdentityIndicatorComponent);
    fixture.detectChanges();
    return { fixture, root: fixture.nativeElement as HTMLElement };
  }

  it('preserves the signed-in identity and role', () => {
    const { root } = create(true);
    expect(root.querySelector('.identity-email')?.textContent).toBe('person@example.test');
    expect(root.querySelector('.identity-role')?.textContent?.trim()).toBe('Admin');
  });

  it('keeps live announcements on the identity text, not the user menu panel', () => {
    const { root } = create(true);
    const identity = root.querySelector<HTMLElement>('[aria-live="polite"]');
    expect(identity).not.toBeNull();
    expect(root.querySelector('details')?.hasAttribute('aria-live')).toBeFalse();
    expect(identity?.textContent).toContain('person@example.test');
    expect(identity?.textContent).toContain('Admin');
    expect(identity?.textContent).not.toContain('Setup');
    expect(identity?.querySelector('.identity-panel')).toBeNull();
  });

  for (const failed of [false, true]) {
    it(`does not expose Setup to a non-admin${failed ? ' when identity lookup fails' : ''}`, () => {
      const { root } = create(false, failed);
      expect(root.querySelector('a[href="/setup"]')).toBeNull();
      expect(root.querySelector('.identity-role')?.textContent?.trim()).toBe('Read-only');
    });
  }

  it('opens with a native keyboard-operable summary and Escape closes with focus restored', () => {
    const { root } = create(true);
    const menu = root.querySelector<HTMLDetailsElement>('details')!;
    expect(menu).not.toBeNull();
    if (!menu) return;
    const trigger = menu.querySelector('summary')!;
    expect(trigger.getAttribute('aria-label')).toBe('User menu, person@example.test, Admin');
    trigger.focus();
    trigger.click();
    expect(menu.open).toBeTrue();
    const setup = menu.querySelector<HTMLAnchorElement>('a')!;
    setup.focus();
    setup.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(menu.open).toBeFalse();
    expect(document.activeElement).toBe(trigger);
  });

  it('closes on navigation or when focus leaves the user menu', () => {
    const { root } = create(true);
    const menu = root.querySelector<HTMLDetailsElement>('details')!;
    expect(menu).not.toBeNull();
    if (!menu) return;
    const trigger = menu.querySelector('summary')!;
    trigger.click();
    menu.querySelector<HTMLAnchorElement>('a')!.click();
    expect(menu.open).toBeFalse();
    trigger.click();
    menu.dispatchEvent(new FocusEvent('focusout', { relatedTarget: document.body, bubbles: true }));
    expect(menu.open).toBeFalse();
  });

  it('keeps the reload notice above the closed menu with a dot on the account row', () => {
    const { fixture, root } = create(false);
    expect(root.querySelector('details')!.open).toBeFalse();
    expect(root.querySelector('.account-update')?.textContent).toContain('New version available');
    expect(root.querySelector('.account-update-dot')).not.toBeNull();
    const version = fixture.debugElement.query(By.directive(VersionFooterComponent)).componentInstance as VersionFooterComponent;
    const reload = spyOn(version, 'reload');
    root.querySelector<HTMLButtonElement>('.account-update button')!.click();
    expect(reload).toHaveBeenCalled();
  });

  it('shows the version and a 44 px copy action inside the account menu', () => {
    const { fixture, root } = create(true);
    root.querySelector('summary')!.click();
    fixture.detectChanges();
    const version = fixture.debugElement.query(By.directive(VersionFooterComponent)).componentInstance as VersionFooterComponent;
    const copy = spyOn(version, 'copy').and.resolveTo();
    const button = root.querySelector<HTMLButtonElement>('.identity-panel button.build')!;
    expect(root.querySelector('.identity-panel')?.textContent).toContain('20261009-0838');
    expect(button.textContent).toContain('Build aaaaaaa');
    button.click();
    expect(copy).toHaveBeenCalled();
    for (const control of root.querySelectorAll<HTMLElement>('summary, .identity-panel a, .identity-panel button')) expectHitBox(control);
    const update = root.querySelector<HTMLElement>('.account-update')!;
    expect(getComputedStyle(update).display).toBe(window.innerWidth >= 1024 ? 'flex' : 'none');
    if (window.innerWidth >= 1024) expectHitBox(update.querySelector<HTMLButtonElement>('button')!);
  });
});
