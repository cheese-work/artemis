/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

import { LessonsComponent } from './pages/lessons/lessons.component';
import { Routes } from '@angular/router';
import { HomeComponent } from './pages/home/home.component';
import { WorkspaceComponent } from './pages/workspace/workspace.component';
import { LegacyWorkspaceComponent } from './pages/legacy-workspace/legacy-workspace.component';
import { firstRunGuard } from './guards/first-run.guard';
import { adminGuard } from './guards/admin.guard';
import { ProvidersComponent } from './pages/providers/providers.component';

export const routes: Routes = [
  { path: '', pathMatch: 'full', redirectTo: 'sessions/new' },
  { path: 'setup', component: HomeComponent },
  { path: 'providers', component: ProvidersComponent, canActivate: [adminGuard] },
  { path: 'sessions', component: WorkspaceComponent, data: { review: true } },
  { path: 'sessions/new', component: WorkspaceComponent, data: { newSession: true }, canActivate: [firstRunGuard] },
  { path: 'sessions/:id', component: WorkspaceComponent, data: { review: true } },
  { path: 'workspace', pathMatch: 'full', redirectTo: 'sessions/new' },
  { path: 'runs', pathMatch: 'full', redirectTo: 'sessions' },
  { path: 'runs/:id', redirectTo: 'sessions/:id' },
  { path: 'lessons', component: LessonsComponent },
  { path: 'check', component: LegacyWorkspaceComponent },
  { path: '**', redirectTo: 'sessions' }
];
