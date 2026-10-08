import { REPORT_SUMMARY_MAX, isLongReport, summarizeReport } from './report-summary.util';

const REPORT = [
  '## Báo cáo kiểm thử',
  '',
  'Đã chạy 12 bước trên thiết bị.',
  '',
  '**Kết quả chính:** Không đăng nhập được vì nút **Tiếp tục** không phản hồi. Xem `log` chi tiết.',
  '',
  '| Bước | Trạng thái |',
  '| --- | --- |',
  '| 1 | Đạt |',
  '| 2 | Lỗi |',
  '',
  '- Ghi chú phụ'
].join('\n');

describe('report summary', () => {
  it('prefers the key-result paragraph and strips markdown', () => {
    expect(summarizeReport(REPORT)).toBe('Kết quả chính: Không đăng nhập được vì nút Tiếp tục không phản hồi. Xem log chi tiết.');
  });

  it('skips headings and tables when there is no key-result label', () => {
    expect(summarizeReport(REPORT.replace('**Kết quả chính:**', 'Tóm lại:'))).toContain('Đã chạy 12 bước');
    expect(summarizeReport('## Title\n\n| a | b |\n| --- | --- |\n\nFirst *real* paragraph.\nSecond line.'))
      .toBe('First real paragraph. Second line.');
  });

  it('caps the summary with an ellipsis', () => {
    const summary = summarizeReport(`## T\n\n${'từ '.repeat(300)}`);
    expect(summary.length).toBeLessThanOrEqual(REPORT_SUMMARY_MAX);
    expect(summary.endsWith('…')).toBeTrue();
  });

  it('leaves a short plain reason unchanged', () => {
    expect(summarizeReport('Target not found')).toBe('Target not found');
    expect(summarizeReport('Missing id login_button_main')).toBe('Missing id login_button_main');
  });

  it('detects long or structured reports only', () => {
    expect(isLongReport('Target not found')).toBeFalse();
    expect(isLongReport('Line one\nLine two')).toBeFalse();
    expect(isLongReport('x'.repeat(REPORT_SUMMARY_MAX + 1))).toBeTrue();
    expect(isLongReport('Failed\n\n## Details\nmore')).toBeTrue();
    expect(isLongReport('Failed\n| a | b |\n| - | - |')).toBeTrue();
  });
});
