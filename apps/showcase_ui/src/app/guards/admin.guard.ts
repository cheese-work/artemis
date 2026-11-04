import { inject } from '@angular/core';
import { CanActivateFn, Router } from '@angular/router';
import { catchError, map, of, take } from 'rxjs';
import { AdminConfigService } from '../services/admin-config.service';

export const adminGuard: CanActivateFn = () => {
  const router = inject(Router);
  return inject(AdminConfigService).getIdentity().pipe(
    take(1),
    map(identity => identity.admin ? true : router.createUrlTree(['/workspace'])),
    catchError(() => of(router.createUrlTree(['/workspace'])))
  );
};
