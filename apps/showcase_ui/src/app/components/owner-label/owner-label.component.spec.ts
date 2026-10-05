import { Component, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { AdminConfigService } from '../../services/admin-config.service';
import { OwnerScopeService } from '../../services/owner-scope.service';
import { OwnerLabelComponent } from './owner-label.component';

@Component({
  standalone: true,
  imports: [OwnerLabelComponent],
  template: '<app-owner-label [owner]="owner()" />'
})
class HostComponent {
  public readonly owner = signal<string | null>('qa@example.test');
}

describe('OwnerLabelComponent', () => {
  function render(admin: boolean) {
    const api = jasmine.createSpyObj<AdminConfigService>('AdminConfigService', ['getIdentity']);
    api.getIdentity.and.returnValue(of({ email: 'a@example.test', admin, auth_mode: 'cloudflare', reason: null }));
    TestBed.configureTestingModule({ providers: [{ provide: AdminConfigService, useValue: api }] });
    const fixture = TestBed.createComponent(HostComponent);
    const scope = TestBed.inject(OwnerScopeService);
    scope.load();
    fixture.detectChanges();
    return { fixture, scope, text: () => (fixture.nativeElement as HTMLElement).textContent?.trim() ?? '' };
  }

  it('shows nothing while an admin looks at only their own runs', () => {
    expect(render(true).text()).toBe('');
  });

  it('shows the owner on every row once an admin turns All users on', () => {
    const { fixture, scope, text } = render(true);
    scope.setAllUsers(true);
    fixture.detectChanges();
    expect(text()).toBe('Owner: qa@example.test');
  });

  it('says "No owner" for a run submitted without an identity', () => {
    const { fixture, scope, text } = render(true);
    fixture.componentInstance.owner.set(null);
    scope.setAllUsers(true);
    fixture.detectChanges();
    expect(text()).toBe('Owner: No owner');
  });

  it('never labels a QAs rows', () => {
    const { fixture, scope, text } = render(false);
    scope.setAllUsers(true);
    fixture.detectChanges();
    expect(text()).toBe('');
  });
});
