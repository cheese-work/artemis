module.exports = function (config) {
  config.set({
    basePath: '',
    frameworks: ['jasmine'],
    plugins: [
      require('karma-jasmine'),
      require('karma-chrome-launcher'),
      require('karma-jasmine-html-reporter'),
      require('karma-coverage')
    ],
    jasmineHtmlReporter: { suppressAll: true },
    coverageReporter: {
      dir: require('node:path').join(__dirname, 'coverage/frontend'),
      subdir: '.',
      reporters: [{ type: 'html' }, { type: 'text-summary' }]
    },
    // The X99 runners share a busy host: a headless browser can stall for a moment.
    browserDisconnectTimeout: 10000,
    browserDisconnectTolerance: 2,
    browserNoActivityTimeout: 120000,
    captureTimeout: 120000,
    reporters: ['progress', 'kjhtml'],
    browsers: ['ChromeHeadlessNarrow', 'ChromeHeadlessWide'],
    customLaunchers: {
      ChromeHeadlessNarrow: {
        base: 'ChromeHeadless',
        displayName: 'ChromeHeadless 800px',
        flags: ['--window-size=800,900']
      },
      ChromeHeadlessWide: {
        base: 'ChromeHeadless',
        displayName: 'ChromeHeadless 1440px',
        flags: ['--window-size=1440,900']
      }
    },
    restartOnFileChange: true
  });
};
