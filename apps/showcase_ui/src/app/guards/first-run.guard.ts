import { inject } from '@angular/core';
import { CanActivateFn, Router } from '@angular/router';
import { catchError, map, of, take } from 'rxjs';
import { SystemService } from '../services/system.service';

export const firstRunGuard: CanActivateFn = () => {
  const router = inject(Router);
  return inject(SystemService).fetchReadiness().pipe(
    take(1),
    map(report => {
      const config = report.probes.find(probe => probe.id === 'system_config');
      const credentials = report.probes.find(probe => probe.id === 'llm_api_key' || probe.id === 'gemini_api_key');
      const providers = credentials?.metadata?.['providers'];
      const noCredentials = credentials?.metadata?.['is_set'] === false &&
        (!Array.isArray(providers) || providers.every(provider => provider.is_set === false));
      return config?.status === 'fail' || noCredentials ? router.createUrlTree(['/setup']) : true;
    }),
    catchError(() => of(true))
  );
};
