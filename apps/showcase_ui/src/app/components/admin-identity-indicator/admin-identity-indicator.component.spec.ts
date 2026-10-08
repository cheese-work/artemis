import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { of, throwError } from 'rxjs';
import { AdminConfigService } from '../../services/admin-config.service';
import { AdminIdentityIndicatorComponent } from './admin-identity-indicator.component';

describe('AdminIdentityIndicatorComponent', () => {
  function create(admin: boolean, failed = false) {
    TestBed.configureTestingModule({
      imports: [AdminIdentityIndicatorComponent],
      providers: [provideRouter([]), {
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
});
