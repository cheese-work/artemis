import { DOCUMENT } from '@angular/common';
import { HttpInterceptorFn } from '@angular/common/http';
import { inject } from '@angular/core';
import { appUrl } from '../utils/app-url.util';

export const appUrlInterceptor: HttpInterceptorFn = (request, next) => {
  const baseURI = inject(DOCUMENT).baseURI;
  return next(request.clone({ url: appUrl(request.url, baseURI) }));
};
