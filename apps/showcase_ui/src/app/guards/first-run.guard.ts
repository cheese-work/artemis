import { inject } from '@angular/core';
import { CanActivateFn, Router } from '@angular/router';
import { catchError, map, of, switchMap, take } from 'rxjs';
import { SystemService } from '../services/system.service';
import { ModelCatalogService } from '../services/model-catalog.service';

export const firstRunGuard: CanActivateFn = () => {
  const router = inject(Router);
  const models = inject(ModelCatalogService);
  return inject(SystemService).fetchReadiness().pipe(
    take(1),
    switchMap(report => {
      const config = report.probes.find(probe => probe.id === 'system_config');
      const credentials = report.probes.find(probe => probe.id === 'llm_api_key' || probe.id === 'gemini_api_key');
      const providers = credentials?.metadata?.['providers'];
      const noCredentials = credentials?.metadata?.['is_set'] === false &&
        (!Array.isArray(providers) || providers.every(provider => provider.is_set === false));
      if (config?.status === 'fail') return of(router.createUrlTree(['/setup']));
      if (noCredentials) return models.fetch().pipe(
        map(catalog => catalog.models.length > 0 ? true : router.createUrlTree(['/setup'])),
        catchError(() => of(router.createUrlTree(['/setup'])))
      );
      return of(true);
    }),
    catchError(() => of(true))
  );
};
