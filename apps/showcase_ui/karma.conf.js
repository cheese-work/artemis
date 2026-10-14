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
