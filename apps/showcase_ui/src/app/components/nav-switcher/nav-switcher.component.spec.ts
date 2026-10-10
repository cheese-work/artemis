import { TestBed } from '@angular/core/testing';
import { HttpClient } from '@angular/common/http';
import { provideRouter, Router } from '@angular/router';
import { Component } from '@angular/core';
import { phone, phoneFakes } from '../../testing/phone-fakes';
import { of } from 'rxjs';
import { AdminConfigService } from '../../services/admin-config.service';
import { NavSwitcherComponent } from './nav-switcher.component';
import { expectHitBox } from '../../testing/hit-box';
