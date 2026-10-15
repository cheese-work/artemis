import { Component, Type } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter, Router } from '@angular/router';
import { of, throwError } from 'rxjs';
import { routes } from '../../app.routes';
import { RegistryDevice } from '../../core/models/host.model';
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
const inUse = device({});
const idle = device({ serial: '93AX01X9Q', model: 'Pixel 3a', source: 'browser', owner: 'qa@example.test', computer_id: null, computer_name: null });
const off = device({ serial: 'f3a1c09e', model: 'Xiaomi 10', computer_id: 'c2', computer_name: 'Thean PC', computer_status: 'offline', reason: 'agent_disconnected' });
const listing = (devices: RegistryDevice[]) => ({ enabled: true, hosts: [], devices });

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
    const button = (name: string) => Array.from(root.querySelectorAll<HTMLButtonElement>('button')).find(b => b.textContent!.trim() === name);
    return { fixture, root, fakes, button, router: TestBed.inject(Router) };
  }
  const rows = (root: HTMLElement) => Array.from(root.querySelectorAll<HTMLElement>('.phone-row'));

  it('is a route outside Setup', () => {
    expect(routes.find(route => route.path === 'phones')).toBeDefined();
  });

  it('lists one row per phone: model as the name, serial beneath, a plain status', async () => {
    const { root } = await create();
    expect(root.querySelector('h2')?.textContent).toContain('Phones');
    expect(rows(root).map(row => row.querySelector('.phone-name')?.textContent?.trim())).toEqual(['Pixel 6 Pro', 'Pixel 3a', 'Xiaomi 10']);
    expect(rows(root).map(row => row.querySelector('.phone-serial')?.textContent?.trim())).toEqual([inUse.serial, idle.serial, off.serial]);
    expect(rows(root).map(row => row.querySelector('.phone-status')?.textContent?.trim())).toEqual(['In use', 'Idle', 'Disconnected']);
  });

  it('opens the first phone in the detail pane and follows the selected row', async () => {
    const { fixture, root } = await create();
    expect(rows(root)[0].getAttribute('aria-current')).toBe('true');
    expect(root.querySelector('.phone-detail h3')?.textContent).toContain('Pixel 6 Pro');
    expect(root.querySelector('.phone-detail')?.textContent).toContain('open youtube');
    rows(root)[2].click();
    fixture.detectChanges();
    expect(rows(root)[2].getAttribute('aria-current')).toBe('true');
    expect(rows(root)[0].getAttribute('aria-current')).toBeNull();
    expect(root.querySelector('.phone-detail h3')?.textContent).toContain('Xiaomi 10');
    expect(root.querySelector('.phone-detail')?.textContent).toContain('Disconnected');
  });

  it('starts a new session on the selected phone', async () => {
    const { fixture, fakes, button, router } = await create();
    button('New session on this phone')!.click();
    fixture.detectChanges();
    await fixture.whenStable();
    expect(fakes.system.chooseRunTarget).toHaveBeenCalledOnceWith(inUse.serial);
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
    expect(root.textContent).not.toContain('No phones yet');
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
    expect(root.textContent).toContain('No phones yet. Connect one by USB or start a Cloud emulator.');
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
