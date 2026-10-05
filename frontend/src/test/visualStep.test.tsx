import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import VisualStep from '../components/VisualStep';
import VerificationNotice from '../components/VerificationNotice';

describe('桌面执行证据', () => {
  it('展示操作前后截图、稳定状态、耗时和恢复提示', () => {
    render(<VisualStep taskId="task-1" result={{
      stable: false, timing_ms: { input: 15, settle_capture: 3010 },
      recovery: { no_progress_steps: 3, suggestion: 'reground' },
      trajectory: { step: 1, before: 'step-0001-before.png', after: 'step-0001-after.png', target: 'step-0001-target.png' },
    }} />);
    expect(screen.getByText('画面尚未稳定')).toBeTruthy();
    expect(screen.getByText(/连续无进展 3 步 · 重新定位/)).toBeTruthy();
    expect(screen.getByText(/输入 15 ms/)).toBeTruthy();
    expect(screen.getByAltText('动作前截图与点击标记').getAttribute('src')).toBe('/api/tasks/task-1/artifacts/step-0001-target.png');
    expect(screen.getByAltText('动作后截图').getAttribute('src')).toBe('/api/tasks/task-1/artifacts/step-0001-after.png');
  });

  it('运行结束之外展示独立目标验证证据', () => {
    render(<VerificationNotice verification={{ status: 'unmet', expected: '文件已保存', evidence: '仍显示未保存标记',
      frame_id: 'frame-1', scope: 'task', duration_ms: 100 }} />);
    expect(screen.getByText('目标尚未达到')).toBeTruthy();
    expect(screen.getByText('仍显示未保存标记')).toBeTruthy();
    expect(screen.queryByText('目标已验证')).toBeNull();
  });
});
