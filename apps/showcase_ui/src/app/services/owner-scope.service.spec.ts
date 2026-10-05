import { TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { AdminConfigService, AdminIdentity } from './admin-config.service';
import { OwnerScopeService } from './owner-scope.service';

const identity = (over: Partial<AdminIdentity> = {}): AdminIdentity => ({
  email: 'qa1@example.test',
  admin: false,
  auth_mode: 'cloudflare',
  reason: null,
  ...over
});

describe('OwnerScopeService', () => {
  let admin: jasmine.SpyObj<AdminConfigService>;

  function create(who: AdminIdentity | null): OwnerScopeService {
    admin.getIdentity.and.returnValue(who ? of(who) : throwError(() => new Error('no jwt')));
    const service = TestBed.inject(OwnerScopeService);
    service.load();
    return service;
  }

  beforeEach(() => {
    admin = jasmine.createSpyObj<AdminConfigService>('AdminConfigService', ['getIdentity']);
    TestBed.configureTestingModule({ providers: [{ provide: AdminConfigService, useValue: admin }] });
  });

  it('asks who the caller is once, however many components load it', () => {
    const service = create(identity());
    service.load();
    service.load();
    expect(admin.getIdentity).toHaveBeenCalledTimes(1);
  });

  it('offers the All users switch to admins only, and never in open mode', () => {
    expect(create(identity({ admin: true })).canSwitch()).toBeTrue();
    TestBed.resetTestingModule();
    admin = jasmine.createSpyObj<AdminConfigService>('AdminConfigService', ['getIdentity']);
    TestBed.configureTestingModule({ providers: [{ provide: AdminConfigService, useValue: admin }] });
    expect(create(identity()).canSwitch()).toBeFalse();
    TestBed.resetTestingModule();
    admin = jasmine.createSpyObj<AdminConfigService>('AdminConfigService', ['getIdentity']);
    TestBed.configureTestingModule({ providers: [{ provide: AdminConfigService, useValue: admin }] });
    expect(create(identity({ admin: true, auth_mode: 'open', email: null })).canSwitch()).toBeFalse();
  });

  it('starts with All users off and turns it on only for an admin', () => {
    const service = create(identity({ admin: true }));
    expect(service.allUsers()).toBeFalse();
    service.setAllUsers(true);
    expect(service.allUsers()).toBeTrue();
    service.setAllUsers(false);
    expect(service.allUsers()).toBeFalse();
  });

  it('ignores a request for All users from a non-admin', () => {
    const service = create(identity());
    service.setAllUsers(true);
    expect(service.allUsers()).toBeFalse();
  });

  it('shows nobody else when the identity cannot be read', () => {
    const service = create(null);
    service.setAllUsers(true);
    expect(service.allUsers()).toBeFalse();
    expect(service.canSwitch()).toBeFalse();
  });

  describe('canManage', () => {
    it('lets a QA manage their own run and not another QA\'s', () => {
      const service = create(identity());
      expect(service.canManage('qa1@example.test')).toBeTrue();
      expect(service.canManage('qa2@example.test')).toBeFalse();
    });

    it('treats a run with no owner as read-only for a QA and manageable for an admin', () => {
      expect(create(identity()).canManage(null)).toBeFalse();
      TestBed.resetTestingModule();
      admin = jasmine.createSpyObj<AdminConfigService>('AdminConfigService', ['getIdentity']);
      TestBed.configureTestingModule({ providers: [{ provide: AdminConfigService, useValue: admin }] });
      expect(create(identity({ admin: true })).canManage(null)).toBeTrue();
    });

    it('lets an admin manage any run', () => {
      expect(create(identity({ admin: true })).canManage('qa2@example.test')).toBeTrue();
    });

    it('does not filter in open mode', () => {
      const service = create(identity({ auth_mode: 'open', email: null, admin: true }));
      expect(service.canManage('anyone@example.test')).toBeTrue();
    });

    it('is read-only until the identity is known', () => {
      admin.getIdentity.and.returnValue(of(identity()));
      expect(TestBed.inject(OwnerScopeService).canManage('qa1@example.test')).toBeFalse();
    });
  });
});
