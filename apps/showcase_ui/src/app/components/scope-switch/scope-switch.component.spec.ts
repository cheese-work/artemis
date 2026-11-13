import { TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { AdminConfigService, AdminIdentity } from '../../services/admin-config.service';
import { OwnerScopeService } from '../../services/owner-scope.service';
import { ScopeSwitchComponent } from './scope-switch.component';

describe('ScopeSwitchComponent', () => {
  function render(who: Partial<AdminIdentity>) {
    const admin = jasmine.createSpyObj<AdminConfigService>('AdminConfigService', ['getIdentity']);
    admin.getIdentity.and.returnValue(of({ email: 'a@example.test', admin: false, auth_mode: 'cloudflare', reason: null, ...who }));
    TestBed.configureTestingModule({ providers: [{ provide: AdminConfigService, useValue: admin }] });
    const fixture = TestBed.createComponent(ScopeSwitchComponent);
    fixture.detectChanges();
    return {
      fixture,
      scope: TestBed.inject(OwnerScopeService),
      control: () => fixture.nativeElement.querySelector('[role="switch"]') as HTMLButtonElement | null
    };
  }

  it('is not shown to a QA', () => {
    expect(render({}).control()).toBeNull();
  });

  it('is not shown in open mode', () => {
    expect(render({ email: null, admin: true, auth_mode: 'open' }).control()).toBeNull();
  });

  it('is shown to an admin, labelled "All users" and off by default', () => {
    const { control } = render({ admin: true });
    expect(control()).not.toBeNull();
    expect(control()!.textContent).toContain('All users');
    expect(control()!.getAttribute('aria-checked')).toBe('false');
  });

  it('turns all-users on and off when clicked', () => {
    const { control, scope, fixture } = render({ admin: true });
    control()!.click();
    fixture.detectChanges();
    expect(scope.showAll()).toBeTrue();
    expect(control()!.getAttribute('aria-checked')).toBe('true');
    control()!.click();
    fixture.detectChanges();
    expect(scope.showAll()).toBeFalse();
    expect(control()!.getAttribute('aria-checked')).toBe('false');
  });

  it('is a native button in the tab order, so Tab reaches it and Space or Enter toggles it', () => {
    const { control } = render({ admin: true });
    const el = control()!;
    expect(el.tagName).toBe('BUTTON');
    expect(el.type).toBe('button');
    expect(el.tabIndex).toBe(0);
    expect(el.disabled).toBeFalse();
    el.focus();
    expect(document.activeElement).toBe(el);
  });
});
