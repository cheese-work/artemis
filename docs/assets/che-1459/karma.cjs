module.exports = config => config.set({
  frameworks: ['jasmine'],
  plugins: [require('../../../apps/showcase_ui/node_modules/karma-jasmine'), require('../../../apps/showcase_ui/node_modules/karma-chrome-launcher')],
  customLaunchers: { ScopedChrome: { base: 'ChromeHeadless', flags: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage', '--disable-extensions', '--proxy-server=direct://', '--proxy-bypass-list=*', '--remote-debugging-port=0'] } },
  browsers: ['ScopedChrome'],
  hostname: '127.0.0.1',
  listenAddress: '127.0.0.1',
  port: 18959,
  reporters: ['dots'],
  singleRun: true,
  captureTimeout: 20000
});
