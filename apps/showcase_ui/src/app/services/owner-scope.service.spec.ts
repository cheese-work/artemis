import { TestBed } from '@angular/core/testing';
import { Observable, of, throwError } from 'rxjs';
import { AdminConfigService, AdminIdentity } from './admin-config.service';
import { OwnerScopeService } from './owner-scope.service';

const identity = (over: Partial<AdminIdentity> = {}): AdminIdentity => ({
  email: 'qa@example.test',
  admin: false,
  auth_mode: 'cloudflare',
  reason: null,
  ...over
});

describe('OwnerScopeService', () => {
  let admin: jasmine.SpyObj<AdminConfigService>;

  function create(who: Observable<AdminIdentity>): OwnerScopeService {
    admin.getIdentity.and.returnValue(who);
    const service = TestBed.inject(OwnerScopeService);
    service.load();
    return service;
  }

  beforeEach(() => {
    admin = jasmine.createSpyObj<AdminConfigService>('AdminConfigService', ['getIdentity']);
    TestBed.configureTestingModule({ providers: [{ provide: AdminConfigService, useValue: admin }] });
  });

  it('asks who you are once, however many components load it', () => {
    const service = create(of(identity()));
    service.load();
    service.load();
    expect(admin.getIdentity).toHaveBeenCalledTimes(1);
  });

  it('lets a QA see and act on only their own runs, with no All users switch', () => {
    const service = create(of(identity()));
    expect(service.canSeeAll()).toBeFalse();
    expect(service.canAct('qa@example.test')).toBeTrue();
    expect(service.canAct('other@example.test')).toBeFalse();
    expect(service.canAct(null)).toBeFalse();
  });

  it('never widens a QA to all users, even if the switch state is set', () => {
    const service = create(of(identity()));
    service.setAllUsers(true);
    expect(service.showAll()).toBeFalse();
    expect(service.queryParams()).toEqual({});
  });

  it('gives an admin the switch, off by default, and acts on every run', () => {
    const service = create(of(identity({ email: 'admin@example.test', admin: true })));
    expect(service.canSeeAll()).toBeTrue();
    expect(service.allUsers()).toBeFalse();
    expect(service.showAll()).toBeFalse();
    expect(service.queryParams()).toEqual({});
    expect(service.canAct('qa@example.test')).toBeTrue();
    expect(service.canAct(null)).toBeTrue();
  });

  it('asks the server for scope=all only while an admin has the switch on', () => {
    const service = create(of(identity({ admin: true })));
    service.setAllUsers(true);
    expect(service.showAll()).toBeTrue();
    expect(service.queryParams()).toEqual({ scope: 'all' });
    service.setAllUsers(false);
    expect(service.queryParams()).toEqual({});
  });

  it('changes nothing in open mode: no switch, no filter, every action allowed', () => {
    const service = create(of(identity({ email: null, auth_mode: 'open' })));
    expect(service.canSeeAll()).toBeFalse();
    expect(service.showAll()).toBeFalse();
    expect(service.canAct('anyone@example.test')).toBeTrue();
    expect(service.canAct(null)).toBeTrue();
  });

  it('allows nothing until it knows who you are', () => {
    const service = create(new Observable<AdminIdentity>());
    expect(service.canAct('qa@example.test')).toBeFalse();
    expect(service.canSeeAll()).toBeFalse();
  });

  it('treats a failed identity lookup as a signed-out viewer', () => {
    const service = create(throwError(() => new Error('offline')));
    expect(service.canSeeAll()).toBeFalse();
    expect(service.canAct('qa@example.test')).toBeFalse();
  });

  it('names an unowned run instead of leaving the label blank', () => {
    const service = create(of(identity({ admin: true })));
    expect(service.ownerText('qa@example.test')).toBe('qa@example.test');
    expect(service.ownerText(null)).toBe('No owner');
  });
});
