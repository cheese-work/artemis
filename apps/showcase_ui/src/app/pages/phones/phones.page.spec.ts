import { HttpErrorResponse } from '@angular/common/http';
import { Component, Type } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter, Router } from '@angular/router';
import { of, throwError } from 'rxjs';
import { routes } from '../../app.routes';
import { Computer, RegistryDevice } from '../../core/models/host.model';
import { phoneFakes } from '../../testing/phone-fakes';

@Component({ template: '' })
class SessionStub {}

const device = (over: Partial<RegistryDevice>): RegistryDevice => ({
  serial: '2A091FDH200B2X',
  model: 'Pixel 6 Pro',
  device_kind: 'phone',
  source: 'computer',
  owner: null,
  computer_id: 'c1',
  computer_name: 'Lab Mac',
  computer_status: 'online',
  reason: null,
  since: 1_800_000_000,
  ...over
});
const inUse = device({ device_id: 'dev_pixel6' });
const idle = device({ serial: '93AX01X9Q', model: 'Pixel 3a', source: 'browser', owner: 'qa@example.test', computer_id: null, computer_name: null, device_id: 'dev_pixel3a' });
const off = device({ serial: 'f3a1c09e', model: 'Xiaomi 10', computer_id: 'c2', computer_name: 'Thean PC', computer_status: 'offline', reason: 'agent_disconnected', device_id: 'dev_xiaomi' });
const computers: Computer[] = [
  {
    id: 'c1', name: 'Lab Mac', os: 'macOS', agent_version: '1.2.0', protocol_version: 1,
    status: 'online', reason: null, since: 1_800_000_000, phones_shared: 1,
    phones_not_shared: 0, unshared_serials: [], share_command: '', active_run_count: 1
  },
  {
    id: 'c2', name: 'Thean PC', os: 'Windows', agent_version: '1.2.0', protocol_version: 1,
    status: 'offline', reason: 'agent_disconnected', since: 1_800_000_000, phones_shared: 1,
    phones_not_shared: 0, unshared_serials: [], share_command: '', active_run_count: 0
  }
];
const listing = (devices: RegistryDevice[]) => ({ enabled: true, hosts: computers, devices });

describe('Phones page (Sessions v2.1 C rev-4)', () => {
  async function create(devices: RegistryDevice[] | 'error' = [inUse, idle, off]) {
    const fakes = phoneFakes();
    fakes.hosts.list.and.callFake(() => devices === 'error' ? throwError(() => new Error('down')) : of(listing(devices)));
    fakes.agent.sessions.set([{ session_id: 's1', status: 'running', device_serial: inUse.serial, initial_goal: 'open youtube' } as never]);
    const route = routes.find(candidate => candidate.path === 'phones');
    expect(route).withContext('a /phones route').toBeDefined();
    await TestBed.configureTestingModule({
      imports: [route!.component as Type<unknown>],
      providers: [provideRouter([{ path: 'sessions/new', component: SessionStub }]), ...fakes.providers]
    }).compileComponents();
    const fixture = TestBed.createComponent(route!.component as Type<unknown>);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    const root = fixture.nativeElement as HTMLElement;
    const button = (name: string) => Array.from(root.querySelectorAll<HTMLButtonElement>('button')).find(b => {
      const icon = b.querySelector('.material-symbols-outlined')?.textContent ?? '';
      return b.textContent!.replace(icon, '').trim() === name;
    });
    return { fixture, root, fakes, button, router: TestBed.inject(Router) };
  }
  /** A chip's visible label, without its icon ligature. */
  const chipText = (chip: Element | null) => Array.from(chip?.childNodes ?? []).filter(n => n.nodeType === Node.TEXT_NODE).map(n => n.textContent).join('').trim();
  const rows = (root: HTMLElement) => Array.from(root.querySelectorAll<HTMLElement>('.phone-card'));

  it('is a route outside Setup', () => {
    expect(routes.find(route => route.path === 'phones')).toBeDefined();
  });

  it('shows the designed phone inventory, computer status and borrow form', async () => {
    const { root } = await create();
    expect(root.querySelector('h1')?.textContent?.trim()).toBe('Phones and computers');
    expect(root.querySelectorAll('.phone-card').length).toBe(3);
    expect(root.querySelector('.phone-list')?.textContent).toContain('Pixel 6 Pro');
    expect(root.querySelector('.computer-list')?.textContent).toContain('Lab Mac');
    expect(root.querySelector('.computer-list')?.textContent).toContain('Thean PC');
    expect(root.querySelector('.borrow-panel form[aria-label="Borrow a phone"]')).not.toBeNull();
    expect(root.querySelector('#phone-choice')).not.toBeNull();
    expect(root.querySelector('#purpose')).not.toBeNull();
    expect(root.querySelector('#duration')).not.toBeNull();
  });

  it('shows the queue stage with a supported icon', async () => {
    const { root } = await create();
    const steps = root.querySelector('.borrow-steps');
    expect(steps?.getAttribute('aria-label')).toBe('Borrow flow: phone, purpose, duration, queue, decision');
    expect(steps?.querySelectorAll('.borrow-step')[3].textContent).toContain('Queue');
    expect(steps?.querySelector('.borrow-step:nth-child(4) .material-symbols-outlined')?.textContent?.trim()).toBe('hourglass_empty');
  });

  it('requests the selected phone with its identity, purpose and duration', async () => {
    const { fixture, root, fakes } = await create();
    const form = root.querySelector<HTMLFormElement>('form[aria-label="Borrow a phone"]');
    expect(form).not.toBeNull();
    if (!form) return;
    const phoneChoice = root.querySelector<HTMLSelectElement>('#phone-choice')!;
    const availablePhone = Array.from(phoneChoice.options).find(option => option.textContent?.includes('Pixel 3a'))!;
    phoneChoice.value = availablePhone.value;
    phoneChoice.dispatchEvent(new Event('change', { bubbles: true }));
    const purpose = root.querySelector<HTMLInputElement>('#purpose')!;
    purpose.value = 'Run first-run QA preview';
    purpose.dispatchEvent(new Event('input', { bubbles: true }));
    const duration = root.querySelector<HTMLSelectElement>('#duration')!;
    duration.value = '3600';
    duration.dispatchEvent(new Event('change', { bubbles: true }));
    form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    fixture.detectChanges();
    expect(fakes.hosts.requestLease).toHaveBeenCalledOnceWith('dev_pixel3a', 3600, 'Run first-run QA preview');
    expect(root.textContent).toContain('Lease request submitted.');
  });

  it('starts with an empty purpose so no fixture text is submitted', async () => {
    const { root } = await create();
    expect(root.querySelector<HTMLInputElement>('#purpose')!.value).toBe('');
    expect(root.querySelector<HTMLButtonElement>('button.submit')!.disabled).toBeTrue();
  });

  it('does not offer a phone the server has no device id for', async () => {
    const { root } = await create([{ ...idle, device_id: null }]);
    const options = Array.from(root.querySelector<HTMLSelectElement>('#phone-choice')!.options).map(o => o.textContent?.trim());
    expect(options).toEqual(['No available phones']);
  });

  it('tells the person what to do for each lease error the server can return', async () => {
    const { fixture, root, fakes } = await create();
    const submit = () => {
      root.querySelector<HTMLFormElement>('form[aria-label="Borrow a phone"]')!
        .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
      fixture.detectChanges();
      return root.querySelector('[role="alert"]')?.textContent ?? '';
    };
    const purpose = root.querySelector<HTMLInputElement>('#purpose')!;
    purpose.value = 'Run first-run QA preview';
    purpose.dispatchEvent(new Event('input', { bubbles: true }));
    const failWith = (status: number, code: string) => fakes.hosts.requestLease.and.returnValue(
      throwError(() => new HttpErrorResponse({ status, error: { code, detail: 'x', fix: 'y' } })));

    failWith(404, 'device_leases_disabled');
    expect(submit()).toContain('turned off');
    failWith(404, 'device_unknown');
    expect(submit()).toContain('does not know this phone');
    failWith(503, 'device_store_not_ready');
    expect(submit()).toContain('Try again in a minute');
    failWith(500, 'other');
    expect(submit()).toContain('Retry the request');
  });

  it('shows the new lease on its phone and reloads the list after a request', async () => {
    const { fixture, root, fakes } = await create();
    const purpose = root.querySelector<HTMLInputElement>('#purpose')!;
    purpose.value = 'Check onboarding';
    purpose.dispatchEvent(new Event('input', { bubbles: true }));
    fakes.hosts.requestLease.and.returnValue(of({
      lease_id: 'l1', device_id: 'dev_pixel3a', holder: 'qa@example.test', purpose: 'Check onboarding',
      restricted: false, space_epoch: 1, created_at: 1, expires_at: Date.now() / 1000 + 3600
    }));
    const lists = fakes.hosts.list.calls.count();
    root.querySelector<HTMLFormElement>('form[aria-label="Borrow a phone"]')!
      .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    fixture.detectChanges();
    const card = rows(root).find(row => row.textContent?.includes('Pixel 3a'))!;
    expect(fakes.hosts.list.calls.count()).toBe(lists + 1);
    expect(chipText(card.querySelector('.phone-card-main .status'))).toBe('Held');
    expect(card.querySelector('.lease-details')?.textContent).toContain('Check onboarding');
    expect(card.querySelector<HTMLButtonElement>('button')!.disabled).toBeTrue();
    expect(Array.from(root.querySelector<HTMLSelectElement>('#phone-choice')!.options).map(o => o.textContent?.trim()))
      .toEqual(['No available phones']);
  });

  it('shows the lease holder from a device_busy response', async () => {
    const { fixture, root, fakes } = await create();
    const purpose = root.querySelector<HTMLInputElement>('#purpose')!;
    purpose.value = 'Run first-run QA preview';
    purpose.dispatchEvent(new Event('input', { bubbles: true }));
    fakes.hosts.requestLease.and.returnValue(throwError(() => new HttpErrorResponse({
      status: 409,
      error: {
        code: 'device_busy',
        detail: 'The phone is leased to someone else.',
        holder: { holder: 'qa-owner@example.test', device_id: 'dev_pixel', purpose: 'First-run check', expires_at: 1_800_000_000 }
      }
    })));
    root.querySelector<HTMLFormElement>('form[aria-label="Borrow a phone"]')!
      .dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    fixture.detectChanges();
    expect(root.querySelector('[role="alert"]')?.textContent).toContain('qa-owner@example.test');
  });

  it('lists each phone with its serial and status', async () => {
    const { root } = await create();
    expect(root.querySelector('#phones-title')?.textContent).toContain('Phones');
    expect(rows(root).map(row => row.querySelector('.phone-name strong')?.textContent?.trim())).toEqual(['Pixel 6 Pro', 'Pixel 3a', 'Xiaomi 10']);
    expect(rows(root).map(row => row.querySelector('.phone-serial')?.textContent?.trim())).toEqual([inUse.serial, idle.serial, off.serial]);
    expect(rows(root).map(row => chipText(row.querySelector('.phone-card-main .status')))).toEqual(['Held', 'Available', 'Disconnected']);
    expect(rows(root).every(row => row.querySelectorAll('.status').length === 1)).withContext('one status chip per card').toBeTrue();
  });

  it('shows the active session on its phone card and keeps offline phones unavailable', async () => {
    const { fixture, root } = await create();
    expect(rows(root)[0].textContent).toContain('open youtube');
    expect(rows(root)[2].querySelector('button')?.disabled).toBeTrue();
    fixture.detectChanges();
  });

  it('starts a new session for an available phone', async () => {
    const { fixture, fakes, button, router } = await create();
    const availableCard = rows(fixture.nativeElement as HTMLElement).find(row => row.textContent?.includes('Pixel 3a'))!;
    availableCard.querySelector<HTMLButtonElement>('button')!.click();
    fixture.detectChanges();
    await fixture.whenStable();
    expect(fakes.system.chooseRunTarget).toHaveBeenCalledOnceWith(idle.serial);
    expect(router.url).toBe('/sessions/new');
  });

  it('connects a phone from this browser', async () => {
    const { fakes, button } = await create();
    button('Connect')!.click();
    expect(fakes.relay.connect).toHaveBeenCalledTimes(1);
  });

  it('lists a phone that connects after the page opened (CHE-1657 review P2)', async () => {
    const { fixture, root, fakes, button } = await create([]);
    expect(rows(root).length).toBe(0);
    fakes.hosts.list.and.returnValue(of(listing([idle])));
    button('Connect')!.click();
    fakes.relay.state.set({ status: 'connected', serial: idle.serial, sessionId: 'bridge', error: null });
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(rows(root).map(row => row.querySelector('.phone-serial')?.textContent?.trim())).toEqual([idle.serial]);
    expect(root.textContent).not.toContain('No phones are enrolled');
  });

  it('shows why a connection failed beside Connect and keeps Connect available (CHE-1657 review P2)', async () => {
    const { fixture, root, fakes, button } = await create([]);
    fakes.relay.state.set({ status: 'idle', serial: null, sessionId: null, error: 'No phone was chosen.' });
    fixture.detectChanges();
    const alert = root.querySelector('[role=alert]');
    expect(alert?.textContent).toContain('No phone was chosen.');
    expect(button('Connect')!.disabled).toBeFalse();
  });

  it('shows the empty state', async () => {
    const { root } = await create([]);
    expect(rows(root).length).toBe(0);
    expect(root.textContent).toContain('No phones are enrolledConnect one by USB or start a Cloud emulator.');
  });

  it('shows an error state with Retry that loads the list again', async () => {
    const { fixture, root, fakes, button } = await create('error');
    expect(root.textContent).toContain('Phones could not be loaded. Retry.');
    fakes.hosts.list.and.returnValue(of(listing([inUse])));
    button('Retry')!.click();
    fixture.detectChanges();
    expect(rows(root).length).toBe(1);
    expect(root.textContent).not.toContain('Phones could not be loaded');
  });
});
