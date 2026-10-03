import { routes } from './app.routes';
import { HomeComponent } from './pages/home/home.component';
import { WorkspaceComponent } from './pages/workspace/workspace.component';

describe('app routes', () => {
  it('redirects the root path to the workspace', () => {
    expect(routes.find(route => route.path === '')?.redirectTo).toBe('workspace');
  });

  it('serves system setup at /setup and workspace at /workspace', () => {
    expect(routes.find(route => route.path === 'setup')?.component).toBe(HomeComponent);
    expect(routes.find(route => route.path === 'workspace')?.component).toBe(WorkspaceComponent);
  });

  it('redirects unknown paths to the workspace', () => {
    expect(routes.find(route => route.path === '**')?.redirectTo).toBe('workspace');
  });
});
