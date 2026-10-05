import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import MarkdownAnswer from '../components/MarkdownAnswer';

describe('Agent Markdown 回答', () => {
  it('渲染标题、强调、表格、列表、引用、代码和链接', () => {
    const content = [
      '## 微信磁盘占用情况', '',
      '| 位置 | 占用 |', '| --- | ---: |', '| `D:\\WeChatFile` | **19.44 GB** |', '',
      '### 说明与建议', '', '- 第一项', '- 第二项', '',
      '> 保留聊天记录。', '',
      '```python', 'print("hello")', '```', '',
      '[项目说明](https://example.com/docs)', '',
      '~~旧建议~~', '', '- [x] 已完成', '- [ ] 待处理',
    ].join('\n');
    const { container } = render(<MarkdownAnswer content={content} />);
    expect(screen.getByRole('heading', { level: 2, name: '微信磁盘占用情况' })).toBeTruthy();
    expect(screen.getByRole('heading', { level: 3, name: '说明与建议' })).toBeTruthy();
    const table = screen.getByRole('table');
    expect(within(table).getByRole('columnheader', { name: '位置' })).toBeTruthy();
    expect(within(table).getByRole('cell', { name: '19.44 GB' }).querySelector('strong')).toBeTruthy();
    expect(within(table).getByText('D:\\WeChatFile').tagName).toBe('CODE');
    expect(container.querySelector('.markdown-table')).toBe(table.parentElement);
    expect(screen.getByText('第一项').tagName).toBe('LI');
    expect(container.querySelector('blockquote')?.textContent).toContain('保留聊天记录。');
    expect(container.querySelector('pre code')?.textContent).toBe('print("hello")\n');
    expect(container.querySelector('del')?.textContent).toBe('旧建议');
    const checkboxes = screen.getAllByRole('checkbox') as HTMLInputElement[];
    expect(checkboxes.map(input => input.checked)).toEqual([true, false]);
    expect(checkboxes.every(input => input.disabled)).toBe(true);
    const link = screen.getByRole('link', { name: '项目说明' });
    expect(link.getAttribute('href')).toBe('https://example.com/docs');
    expect(link.getAttribute('target')).toBe('_blank');
    expect(link.getAttribute('rel')).toBe('noopener noreferrer');
  });

  it('复制保留 Markdown 原文及代码缩进', async () => {
    const user = userEvent.setup();
    const writeText = vi.spyOn(navigator.clipboard, 'writeText').mockResolvedValue();
    const content = '## 标题\n\n**加粗**\n\n```python\nif True:\n    print("hello")\n```';
    render(<MarkdownAnswer content={content} />);
    await user.click(screen.getByRole('button', { name: /复制|Copy/ }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(content));
    writeText.mockRestore();
  });

  it('不执行原始 HTML，也不生成危险协议链接', () => {
    const { container } = render(<MarkdownAnswer content={[
      '<script>alert("test")</script>', '',
      '<img src="x" onerror="alert(1)">', '',
      '[危险链接](javascript:alert%281%29)', '',
      '正常的 **回答**。',
    ].join('\n')} />);
    expect(container.querySelector('script, img, [onerror]')).toBeNull();
    expect(screen.queryByRole('link', { name: '危险链接' })).toBeNull();
    expect(screen.getByText('危险链接').tagName).toBe('SPAN');
    expect(screen.getByText('回答').tagName).toBe('STRONG');
  });
});
