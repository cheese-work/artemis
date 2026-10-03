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

import { Component, signal, computed, inject, OnInit, OnDestroy, ChangeDetectionStrategy } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { SystemService } from '../../services/system.service';
import {
  AdbServerConnectionResult,
  AdbServerDevice,
  DeviceInfo,
  ProbeResult
} from '../../core/models/system.model';
import { UsbPhoneConnectionComponent } from '../../components/usb-phone-connection/usb-phone-connection.component';

type AdbGuideTab = 'emulator' | 'usb' | 'wifi' | 'remote';


@Component({
  selector: 'app-home',
  standalone: true,
  imports: [FormsModule, UsbPhoneConnectionComponent],
  templateUrl: './home.component.html',
  changeDetection: ChangeDetectionStrategy.Eager,
  styleUrl: './home.component.scss'
})
export class HomeComponent implements OnInit, OnDestroy {
  public systemService = inject(SystemService);

  // Interactive guide sub-tab inside the ADB section
  public activeAdbGuideTab = signal<AdbGuideTab>('emulator');
  public emulatorSetupMode = signal<'studio' | 'cli'>('studio');

  // Interactive guide tab for LLM / OCR credentials: 'gemini' | 'ocr'
  public modelSetupMode = signal<'gemini' | 'custom'>('gemini');
  public showOcrConfig = signal<boolean>(false);
  public showFullConfigFile = signal<boolean>(false);

  // Model & Environment configuration from backend
  public modelConfigEnv = computed(() => this.systemService.modelConfigEnv());
  public configWritesLocked = computed(() => this.systemService.configWritesLocked());

  // Google Gemini API Key State
  public geminiKeyInput = signal<string>('');
  public showGeminiKey = signal<boolean>(false);
  public isSavingGeminiKey = signal<boolean>(false);
  public isTestingGeminiKey = signal<boolean>(false);
  public geminiSaveMessage = signal<string | null>(null);
  public geminiSaveError = signal<string | null>(null);

  // Vision OCR API Key State
  public ocrKeyInput = signal<string>('');
  public showOcrKey = signal<boolean>(false);
  public isSavingOcrKey = signal<boolean>(false);
  public isTestingOcrKey = signal<boolean>(false);
  public ocrSaveMessage = signal<string | null>(null);
  public ocrSaveError = signal<string | null>(null);

  // Clipboard copy state tracker for interactive feedback
  public copiedId = signal<string | null>(null);

  // Diagnostic re-check state
  public isRefreshingDiagnostics = signal<boolean>(false);

  // Wireless ADB Interactive connection signals
  public wifiHost = signal<string>('192.168.1.100');
  public wifiPort = signal<string>('5555');
  public isConnectingWifi = signal<boolean>(false);
  public wifiConnectMessage = signal<string | null>(null);
  public wifiConnectError = signal<string | null>(null);
  public adbRestartFeedback = signal<string | null>(null);
  public showConnectionMethods = signal<boolean>(false);

  // ADB server endpoint connection state
  public remoteAdbHost = signal<string>('127.0.0.1');
  public remoteAdbPort = signal<string>('5038');
  public rememberRemoteAdb = signal<boolean>(true);
  public isConnectingRemoteAdb = signal<boolean>(false);
  public isActivatingRemoteAdb = signal<boolean>(false);
  public isSwitchingToLocalAdb = signal<boolean>(false);
  public remoteAdbMessage = signal<string | null>(null);
  public remoteAdbError = signal<string | null>(null);
  public remoteAdbDevices = signal<AdbServerDevice[]>([]);
  public remoteAdbProbeResult = signal<AdbServerConnectionResult | null>(null);
  public adbServerStatus = computed(() => this.systemService.adbServerStatus());
  public isRemoteAdbServer = computed(() => this.systemService.isRemoteAdbServer());
  public remoteAdbHasReadyDevice = computed(() =>
    this.remoteAdbDevices().some(device => device.state === 'device')
  );
  public isProbedRemoteAdbActive = computed(() => {
    const tested = this.remoteAdbProbeResult()?.endpoint;
    const active = this.adbServerStatus()?.endpoint;
    return !!tested && !!active && tested.identity === active.identity;
  });

  public wifiCommand = computed(() => {
    const h = this.wifiHost().trim() || '<phone-ip>';
    const p = this.wifiPort().trim() || '5555';
    return `adb connect ${h}:${p}`;
  });

  // Computed helper states delegating to SystemService
  public isReady = computed(() => this.systemService.isReady());
  public hasReadinessReport = computed(() => this.systemService.hasReadinessReport());
  public isLoading = computed(() => this.systemService.isLoading());
  public isRestartingAdb = computed(() => this.systemService.isRestartingAdb());
  public launchingAvd = computed(() => this.systemService.launchingAvd());
  public emulatorLaunchState = computed(() => this.systemService.emulatorLaunchState());
  public isEmulatorLaunching = computed(() => this.systemService.isEmulatorLaunching());
  public showLaunchLogs = signal<boolean>(false);
  
  // Probes
  public pythonProbe = computed(() => this.systemService.pythonProbe());
  public configProbe = computed(() => this.systemService.configProbe());
  public adbProbe = computed(() => this.systemService.adbProbe());
  public llmProbe = computed(() => this.systemService.llmProbe());
  public geminiProbe = computed(() => this.systemService.geminiProbe());
  public ocrProbe = computed(() => this.systemService.ocrProbe());
  public toolchainProbe = computed(() => this.systemService.toolchainProbe());

  // Step-level readiness
  public isEnvironmentReady = computed(() => this.systemService.isEnvironmentReady());
  public isCredentialsReady = computed(() => this.systemService.isCredentialsReady());
  public isSkipCredentialsCheck = computed(() => this.systemService.isSkipCredentialsCheck());
  public isDeviceReady = computed(() => this.systemService.isDeviceReady());

  // Device information
  public activeDevice = computed(() => this.systemService.activeDevice());
  public connectedDevices = computed(() => this.systemService.connectedDevices());
  public installedAvds = computed(() => this.systemService.installedAvds());
  public emulatorPath = computed(() => this.systemService.emulatorPath());
  public isEmulatorInPath = computed(() => this.systemService.isEmulatorInPath());
  public totalStepCount = computed(() => this.systemService.totalStepCount());
  public passedStepCount = computed(() => this.systemService.passedStepCount());
  public blockerCount = computed(() => this.systemService.blockerCount());
  public passedBlockerCount = computed(() => this.systemService.passedBlockerCount());

  // Configured LLM providers from probe metadata
  public configuredLlmProviders = computed<any[]>(() => {
    const meta = this.llmProbe()?.metadata;
    if (meta && Array.isArray(meta['providers'])) {
      return meta['providers'];
    }
    return [];
  });

  // Multi-OS detection & active OS selection
  public selectedOs = signal<'linux' | 'darwin' | 'windows' | null>(null);
  public effectiveOs = computed<'linux' | 'darwin' | 'windows'>(() => {
    return this.selectedOs() || this.systemService.osType();
  });

  public oneClickSetupCmd = computed(() => {
    const os = this.effectiveOs();
    if (os === 'windows') {
      return 'powershell -ExecutionPolicy Bypass -File scripts/install_deps.ps1';
    }
    return 'bash scripts/install_deps.sh';
  });

  public adbInstallCmd = computed(() => {
    const os = this.effectiveOs();
    if (os === 'windows') {
      return 'winget install Google.PlatformTools';
    }
    if (os === 'darwin') {
      return 'brew install android-platform-tools';
    }
    return 'sudo apt-get install -y adb';
  });

  public toolchainInstallCmd = computed(() => {
    const os = this.effectiveOs();
    if (os === 'windows') {
      return 'winget install Gyan.FFmpeg Genymobile.scrcpy';
    }
    if (os === 'darwin') {
      return 'brew install ffmpeg scrcpy';
    }
    return 'sudo apt-get install -y ffmpeg scrcpy';
  });

  public emuHypervisorTitle = computed(() => {
    const os = this.effectiveOs();
    if (os === 'windows') return 'Enable Windows Hypervisor Platform (WHPX)';
    if (os === 'darwin') return 'Verify macOS Hypervisor / Install Tools';
    return 'Enable KVM Hardware Acceleration (Linux)';
  });

  public emuHypervisorDesc = computed(() => {
    const os = this.effectiveOs();
    if (os === 'windows') return 'Enable Windows Hypervisor Platform in PowerShell (Run as Administrator):';
    if (os === 'darwin') return 'macOS uses native Hypervisor.framework. Install SDK tools via Homebrew (or Studio):';
    return 'Ensure virtualization permissions are granted to your user account:';
  });

  public emuHypervisorCmd = computed(() => {
    const os = this.effectiveOs();
    if (os === 'windows') return 'Enable-WindowsOptionalFeature -Online -FeatureName HypervisorPlatform';
    if (os === 'darwin') return 'brew install --cask android-commandlinetools';
    return 'sudo apt-get install -y qemu-kvm libvirt-daemon-system && sudo adduser $USER kvm';
  });

  public emuSdkInstallCmd = computed(() => {
    const os = this.effectiveOs();
    if (os === 'darwin') {
      return 'sdkmanager --install "system-images;android-34;google_apis;arm64-v8a" "emulator" "platform-tools"';
    }
    return 'sdkmanager --install "system-images;android-34;google_apis;x86_64" "emulator" "platform-tools"';
  });

  public emuCreateAvdCmd = computed(() => {
    const os = this.effectiveOs();
    if (os === 'darwin') {
      return 'avdmanager create avd -n Pixel_8_API_34 -k "system-images;android-34;google_apis;arm64-v8a" --device "pixel_8"';
    }
    return 'avdmanager create avd -n Pixel_8_API_34 -k "system-images;android-34;google_apis;x86_64" --device "pixel_8"';
  });

  // Flag indicating whether Google Cloud Vision OCR is configured
  public isOcrConfigured = computed<boolean>(() => {
    const meta = this.ocrProbe()?.metadata;
    return meta?.['is_set'] === true;
  });

  public isGeminiConfigured = computed<boolean>(() =>
    this.configuredLlmProviders().some(provider => provider.provider === 'google' && provider.is_set === true)
  );

  public hasGeminiKeyInput = computed<boolean>(() => this.geminiKeyInput().trim().length > 0);
  public hasOcrKeyInput = computed<boolean>(() => this.ocrKeyInput().trim().length > 0);

  private focusListener = () => {
    // Silently re-check environment when user returns to the browser tab
    this.systemService.fetchReadiness().subscribe();
  };

  ngOnInit(): void {

    // Initial fetch of system readiness & model configuration
    this.systemService.fetchReadiness().subscribe();
    this.systemService.fetchModelConfigEnv().subscribe();
    this.systemService.fetchCredentialStatus().subscribe();
    this.systemService.fetchAdbServerStatus().subscribe({
      next: status => {
        if (status.endpoint.mode === 'remote') {
          this.remoteAdbHost.set(status.endpoint.host);
          this.remoteAdbPort.set(String(status.endpoint.port));
          this.activeAdbGuideTab.set('remote');
        }
      },
      error: () => {}
    });

    window.addEventListener('focus', this.focusListener);
  }

  ngOnDestroy(): void {
    window.removeEventListener('focus', this.focusListener);
  }

  public setSelectedOs(os: 'linux' | 'darwin' | 'windows'): void {
    this.selectedOs.set(os);
  }

  public setAdbGuideTab(tab: AdbGuideTab): void {
    this.activeAdbGuideTab.set(tab);
    this.remoteAdbError.set(null);
    this.remoteAdbMessage.set(null);
  }

  public setEmulatorSetupMode(mode: 'studio' | 'cli'): void {
    this.emulatorSetupMode.set(mode);
  }

  public setModelSetupMode(mode: 'gemini' | 'custom'): void {
    this.modelSetupMode.set(mode);
    if (mode === 'custom') {
      this.systemService.setSkipCredentialsCheck(true);
      this.systemService.fetchModelConfigEnv().subscribe();
    } else {
      this.systemService.setSkipCredentialsCheck(false);
    }
  }

  public toggleFullConfigFile(): void {
    this.showFullConfigFile.update(v => !v);
  }

  public toggleGeminiKeyVisibility(): void {
    this.showGeminiKey.update(v => !v);
  }

  public toggleOcrKeyVisibility(): void {
    this.showOcrKey.update(v => !v);
  }

  public toggleOcrConfig(): void {
    this.showOcrConfig.update(v => !v);
  }

  public onGeminiKeyChange(val: string): void {
    this.geminiKeyInput.set(val);
    this.geminiSaveError.set(null);
    this.geminiSaveMessage.set(null);
  }

  public onOcrKeyChange(val: string): void {
    this.ocrKeyInput.set(val);
    this.ocrSaveError.set(null);
    this.ocrSaveMessage.set(null);
  }

  public saveGeminiKey(): void {
    const key = this.geminiKeyInput().trim();
    if (!key) return;
    this.isSavingGeminiKey.set(true);
    this.geminiSaveError.set(null);
    this.geminiSaveMessage.set(null);

    this.systemService.updateApiKey('google', key, true).subscribe({
      next: (res) => {
        this.isSavingGeminiKey.set(false);
        this.geminiSaveMessage.set(res?.message || '✓ Gemini API key verified & saved successfully.');
        setTimeout(() => this.geminiSaveMessage.set(null), 5000);
      },
      error: (err) => {
        this.isSavingGeminiKey.set(false);
        this.geminiSaveError.set(err?.error?.detail || err?.message || 'Failed to update Gemini API key.');
      }
    });
  }

  public clearGeminiKey(): void {
    this.geminiKeyInput.set('');
    this.geminiSaveError.set(null);
    this.geminiSaveMessage.set(null);

    if (this.isGeminiConfigured()) {
      this.isSavingGeminiKey.set(true);
      this.systemService.updateApiKey('google', '', true).subscribe({
        next: (res) => {
          this.isSavingGeminiKey.set(false);
          this.geminiSaveMessage.set(res?.message || '✓ Gemini API key cleared.');
          setTimeout(() => this.geminiSaveMessage.set(null), 5000);
        },
        error: (err) => {
          this.isSavingGeminiKey.set(false);
          this.geminiSaveError.set(err?.error?.detail || err?.message || 'Failed to clear Gemini API key.');
        }
      });
    } else {
      this.geminiSaveMessage.set('✓ Gemini API key cleared.');
      setTimeout(() => this.geminiSaveMessage.set(null), 3000);
    }
  }

  public saveOcrKey(): void {
    const key = this.ocrKeyInput().trim();
    if (!key) return;
    this.isSavingOcrKey.set(true);
    this.ocrSaveError.set(null);
    this.ocrSaveMessage.set(null);

    this.systemService.updateApiKey('ocr', key, true).subscribe({
      next: (res) => {
        this.isSavingOcrKey.set(false);
        this.ocrSaveMessage.set(res?.message || '✓ Vision OCR API key verified & saved.');
        setTimeout(() => this.ocrSaveMessage.set(null), 5000);
      },
      error: (err) => {
        this.isSavingOcrKey.set(false);
        this.ocrSaveError.set(err?.error?.detail || err?.message || 'Failed to update Vision OCR key.');
      }
    });
  }

  public clearOcrKey(): void {
    this.ocrKeyInput.set('');
    this.ocrSaveError.set(null);
    this.ocrSaveMessage.set(null);

    if (this.isOcrConfigured()) {
      this.isSavingOcrKey.set(true);
      this.systemService.updateApiKey('ocr', '', true).subscribe({
        next: (res) => {
          this.isSavingOcrKey.set(false);
          this.ocrSaveMessage.set(res?.message || '✓ Vision OCR API key cleared.');
          setTimeout(() => this.ocrSaveMessage.set(null), 5000);
        },
        error: (err) => {
          this.isSavingOcrKey.set(false);
          this.ocrSaveError.set(err?.error?.detail || err?.message || 'Failed to clear Vision OCR key.');
        }
      });
    } else {
      this.ocrSaveMessage.set('✓ Vision OCR API key cleared.');
      setTimeout(() => this.ocrSaveMessage.set(null), 3000);
    }
  }

  public testGeminiKey(): void {
    const key = this.geminiKeyInput().trim();
    if (!key) return;
    this.isTestingGeminiKey.set(true);
    this.geminiSaveError.set(null);
    this.geminiSaveMessage.set(null);

    this.systemService.testApiKey('google', key).subscribe({
      next: (res) => {
        this.isTestingGeminiKey.set(false);
        if (res?.valid) {
          this.geminiSaveMessage.set(res?.message || '✓ Gemini API key is valid!');
        } else {
          this.geminiSaveError.set(res?.message || 'Gemini API key verification failed.');
        }
        setTimeout(() => this.geminiSaveMessage.set(null), 5000);
      },
      error: (err) => {
        this.isTestingGeminiKey.set(false);
        this.geminiSaveError.set(err?.error?.detail || err?.message || 'Gemini API key test failed.');
      }
    });
  }

  public testOcrKey(): void {
    const key = this.ocrKeyInput().trim();
    if (!key) return;
    this.isTestingOcrKey.set(true);
    this.ocrSaveError.set(null);
    this.ocrSaveMessage.set(null);

    this.systemService.testApiKey('ocr', key).subscribe({
      next: (res) => {
        this.isTestingOcrKey.set(false);
        if (res?.valid) {
          this.ocrSaveMessage.set(res?.message || '✓ Vision OCR API key is valid!');
        } else {
          this.ocrSaveError.set(res?.message || 'Vision OCR API key verification failed.');
        }
        setTimeout(() => this.ocrSaveMessage.set(null), 5000);
      },
      error: (err) => {
        this.isTestingOcrKey.set(false);
        this.ocrSaveError.set(err?.error?.detail || err?.message || 'Vision OCR API key test failed.');
      }
    });
  }

  public getProviderDisplayName(tab: string): string {
    switch (tab) {
      case 'gemini': return 'Gemini';
      case 'ocr': return 'Vision OCR';
      default: return tab;
    }
  }

  public getProviderEnvVar(tab: string): string {
    switch (tab) {
      case 'gemini': return 'GEMINI_API_KEY';
      case 'ocr': return 'GOOGLE_VISION_API_KEY';
      default: return 'API_KEY';
    }
  }

  public getProviderHint(tab: string): string {
    switch (tab) {
      case 'gemini':
        return 'For a quick start, Google Gemini provides a free API key. SmartQA also supports other models (OpenAI, Claude, OpenRouter, etc.)—you can configure your own API keys directly in .env or your environment.';
      case 'ocr':
        return 'Google Cloud Vision API key for on-screen OCR text detection and UI grounding.';
      default:
        return 'Configure your API key or use environment definitions.';
    }
  }

  public skipCredentialsCheck(): void {
    this.systemService.skipCredentialsCheck();
  }

  public getApiKeyPlaceholder(tab: string): string {
    switch (tab) {
      case 'gemini': return 'Enter Gemini API Key (e.g. AIzaSy...)';
      case 'ocr': return 'Enter Vision OCR API Key (e.g. AIzaSy...)';
      default: return 'Enter API Key...';
    }
  }

  public isTabProviderActive(tab: string): boolean {
    if (tab === 'ocr') {
      return this.isOcrConfigured();
    }
    const targetProvider = tab === 'gemini' ? 'google' : tab;
    const providers = this.configuredLlmProviders();
    return providers.some(p => p.provider === targetProvider);
  }

  public refreshReadiness(): void {
    this.isRefreshingDiagnostics.set(true);
    this.systemService.fetchReadiness(false, true).subscribe({
      next: () => {
        this.systemService.fetchModelConfigEnv().subscribe({
          next: () => {
            setTimeout(() => this.isRefreshingDiagnostics.set(false), 450);
          },
          error: () => this.isRefreshingDiagnostics.set(false)
        });
      },
      error: () => this.isRefreshingDiagnostics.set(false)
    });
  }

  public restartAdbServer(): void {
    this.adbRestartFeedback.set(null);
    this.systemService.restartAdb().subscribe({
      next: (res) => {
        this.adbRestartFeedback.set(
          res?.restart_result?.skipped ? 'Devices Refreshed ✓' : 'ADB Refreshed ✓'
        );
        setTimeout(() => this.adbRestartFeedback.set(null), 2500);
      },
      error: () => {
        this.adbRestartFeedback.set('Restart Failed');
        setTimeout(() => this.adbRestartFeedback.set(null), 3000);
      }
    });
  }

  public connectWifiDevice(): void {
    if (this.isRemoteAdbServer()) {
      this.wifiConnectError.set('Switch to local ADB before connecting a Wireless ADB device.');
      return;
    }
    const host = this.wifiHost().trim();
    const portStr = this.wifiPort().trim() || '5555';
    const port = parseInt(portStr, 10) || 5555;

    if (!host) {
      this.wifiConnectError.set('Please enter a valid IP address.');
      return;
    }

    this.isConnectingWifi.set(true);
    this.wifiConnectError.set(null);
    this.wifiConnectMessage.set(null);

    this.systemService.connectWirelessAdb(host, port).subscribe({
      next: (res) => {
        this.isConnectingWifi.set(false);
        const cr = res?.connect_result;
        if (cr?.success) {
          this.wifiConnectMessage.set(`Connected to ${host}:${port}!`);
          setTimeout(() => this.wifiConnectMessage.set(null), 4000);
        } else {
          this.wifiConnectError.set(cr?.message || 'Connection failed. Please check phone IP & Wi-Fi.');
        }
      },
      error: (err) => {
        this.isConnectingWifi.set(false);
        this.wifiConnectError.set(err?.error?.detail || 'Failed to connect. Please check adb connection.');
      }
    });
  }

  public connectRemoteAdbServer(): void {
    const host = this.remoteAdbHost().trim();
    const port = Number(this.remoteAdbPort().trim());

    if (!host) {
      this.remoteAdbError.set('Enter the host name or IP address of the ADB server.');
      return;
    }
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      this.remoteAdbError.set('Enter a port between 1 and 65535.');
      return;
    }

    this.isConnectingRemoteAdb.set(true);
    this.remoteAdbError.set(null);
    this.remoteAdbMessage.set(null);
    this.remoteAdbDevices.set([]);
    this.remoteAdbProbeResult.set(null);

    this.systemService.probeAdbServer(host, port).subscribe({
      next: response => {
        this.isConnectingRemoteAdb.set(false);
        const result = response.connection_result;
        if (result.success) {
          this.remoteAdbProbeResult.set(result);
          this.remoteAdbDevices.set(result.devices || []);
          this.remoteAdbMessage.set(result.message);
        } else {
          this.remoteAdbError.set(result.message);
        }
      },
      error: error => {
        this.isConnectingRemoteAdb.set(false);
        this.remoteAdbError.set(
          error?.error?.detail || 'Unable to test the ADB server endpoint.'
        );
      }
    });
  }

  public activateRemoteAdbServer(): void {
    const tested = this.remoteAdbProbeResult();
    if (!tested?.success) {
      this.remoteAdbError.set('Test the endpoint before using it.');
      return;
    }

    this.isActivatingRemoteAdb.set(true);
    this.remoteAdbError.set(null);
    this.systemService.connectAdbServer(
      tested.endpoint.host,
      tested.endpoint.port,
      this.rememberRemoteAdb()
    ).subscribe({
      next: response => {
        this.isActivatingRemoteAdb.set(false);
        const result = response.connection_result;
        if (result.success) {
          this.remoteAdbProbeResult.set(result);
          this.remoteAdbDevices.set(result.devices || []);
          this.remoteAdbMessage.set(result.message);
        } else {
          this.remoteAdbError.set(result.message);
        }
      },
      error: error => {
        this.isActivatingRemoteAdb.set(false);
        this.remoteAdbError.set(
          error?.error?.detail || 'Unable to use the ADB server endpoint.'
        );
      }
    });
  }

  public updateRemoteAdbHost(value: string): void {
    this.remoteAdbHost.set(value);
    this.clearRemoteAdbProbe();
  }

  public updateRemoteAdbPort(value: string): void {
    this.remoteAdbPort.set(value);
    this.clearRemoteAdbProbe();
  }

  private clearRemoteAdbProbe(): void {
    this.remoteAdbProbeResult.set(null);
    this.remoteAdbDevices.set([]);
    this.remoteAdbMessage.set(null);
    this.remoteAdbError.set(null);
  }

  public switchToLocalAdbServer(): void {
    this.isSwitchingToLocalAdb.set(true);
    this.remoteAdbError.set(null);
    this.systemService.useLocalAdbServer(true).subscribe({
      next: response => {
        this.isSwitchingToLocalAdb.set(false);
        this.remoteAdbDevices.set([]);
        this.remoteAdbMessage.set(response.connection_result.message);
      },
      error: error => {
        this.isSwitchingToLocalAdb.set(false);
        this.remoteAdbError.set(
          error?.error?.detail || 'Unable to switch back to the local ADB server.'
        );
      }
    });
  }

  public toggleConnectionMethods(): void {
    this.showConnectionMethods.update(value => !value);
  }

  public launchAvdEmulator(avdName: string): void {
    if (this.isRemoteAdbServer()) {
      return;
    }
    this.systemService.launchEmulator(avdName).subscribe();
  }

  public toggleLaunchLogs(): void {
    this.showLaunchLogs.update(v => !v);
  }

  public stopEmulator(): void {
    this.systemService.stopEmulator().subscribe();
  }

  public dismissEmulatorStatus(): void {
    this.systemService.dismissEmulatorStatus().subscribe();
  }

  public selectTargetDevice(serial: string): void {
    this.systemService.selectDevice(serial).subscribe();
  }

  public getEmulatorCommand(avdName: string): string {
    const p = this.emulatorPath();
    const cmd = this.isEmulatorInPath() ? 'emulator' : (p || '~/Android/Sdk/emulator/emulator');
    return `${cmd} -avd ${avdName}`;
  }

  public copyToClipboard(text: string, id: string): void {
    if (navigator.clipboard) {
      navigator.clipboard.writeText(text).then(() => {
        this.copiedId.set(id);
        setTimeout(() => {
          if (this.copiedId() === id) {
            this.copiedId.set(null);
          }
        }, 2000);
      });
    }
  }

}
