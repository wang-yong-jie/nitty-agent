import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import RunSettings, { defaultOptions } from '../components/RunSettings';
import EventTimeline from '../components/EventTimeline';

describe('运行设置和事件详情', () => {
  it('关闭桌面时清理桌面专属配置，避免提交非法组合', async () => {
    const onChange = vi.fn();
    render(<RunSettings value={{ ...defaultOptions, desktop: true, vision_mode: 'separate',
      vision_model: 'vlm', vision_provider: 'openai', with_local_tools: true }} onChange={onChange}
      capabilities={{ providers: { deepseek: true }, desktop_available: true,
        single_task: true, default_provider: 'deepseek', default_model: 'deepseek-flash' }} />);
    await userEvent.click(screen.getByRole('switch', { name: '启用桌面操作' }));
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ desktop: false,
      vision_mode: 'direct', vision_model: null, vision_provider: null, with_local_tools: false }));
  });

  it('错误事件可展开查看错误分类和部分副作用信息', async () => {
    render(<EventTimeline events={[{ id: 1, task_id: 'task', data: {
      event: 'tool_finished', tool: 'desktop_click', status: 'failed', turn: 2,
      error_info: { code: 'POST_ACTION_CAPTURE_FAILED', side_effects: 'possible' },
    } }]} />);
    await userEvent.click(screen.getByText('工具执行结束'));
    expect(screen.getByText(/POST_ACTION_CAPTURE_FAILED/).textContent).toContain('possible');
  });
});
