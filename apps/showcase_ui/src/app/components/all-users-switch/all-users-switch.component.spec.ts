import { signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { OwnerScopeService } from '../../services/owner-scope.service';
import { AllUsersSwitchComponent } from './all-users-switch.component';

describe('AllUsersSwitchComponent', () => {
  let canSwitch: ReturnType<typeof signal<boolean>>;
  let allUsers: ReturnType<typeof signal<boolean>>;
  let scope: { load: jasmine.Spy; canSwitch: typeof canSwitch; allUsers: typeof allUsers; setAllUsers: jasmine.Spy };
  let fixture: ComponentFixture<AllUsersSwitchComponent>;

  const toggle = () => (fixture.nativeElement as HTMLElement).querySelector<HTMLButtonElement>('button[role="switch"]');

  beforeEach(async () => {
    canSwitch = signal(true);
    allUsers = signal(false);
    scope = {
      load: jasmine.createSpy('load'),
      canSwitch,
      allUsers,
      setAllUsers: jasmine.createSpy('setAllUsers').and.callFake((on: boolean) => allUsers.set(on))
    };
    await TestBed.configureTestingModule({
      imports: [AllUsersSwitchComponent],
      providers: [{ provide: OwnerScopeService, useValue: scope }]
    }).compileComponents();
    fixture = TestBed.createComponent(AllUsersSwitchComponent);
    fixture.detectChanges();
  });

  it('loads who the caller is so the switch can decide to show itself', () => {
    expect(scope.load).toHaveBeenCalled();
  });

  it('shows an admin a labelled switch that starts off', () => {
    expect(toggle()).not.toBeNull();
    expect(toggle()!.getAttribute('aria-checked')).toBe('false');
    expect((fixture.nativeElement as HTMLElement).textContent).toContain('All users');
  });

  it('shows nothing to a QA who is not an admin', () => {
    canSwitch.set(false);
    fixture.detectChanges();
    expect(toggle()).toBeNull();
    expect((fixture.nativeElement as HTMLElement).textContent).not.toContain('All users');
  });

  it('turns on and off with a click and reports its state', () => {
    toggle()!.click();
    fixture.detectChanges();
    expect(scope.setAllUsers).toHaveBeenCalledWith(true);
    expect(toggle()!.getAttribute('aria-checked')).toBe('true');
    toggle()!.click();
    fixture.detectChanges();
    expect(scope.setAllUsers).toHaveBeenCalledWith(false);
    expect(toggle()!.getAttribute('aria-checked')).toBe('false');
  });

  it('is operable from the keyboard: one tab stop, a real button, named by its visible label', () => {
    const button = toggle()!;
    expect(button.tagName).toBe('BUTTON');
    expect(button.type).toBe('button');
    expect(button.tabIndex).toBe(0);
    button.focus();
    expect(document.activeElement).toBe(button);
    const label = button.getAttribute('aria-labelledby');
    expect(label).toBeTruthy();
    expect(fixture.nativeElement.querySelector(`#${label}`).textContent).toContain('All users');
  });
});
