import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import TaskProgressPanel from '../components/TaskProgressPanel';
import { defaultOptions } from '../components/RunSettings';
import type { Task } from '../api/client';

describe('持久化任务进展', () => {
  it('区分已验收与待处理步骤，显示快照和待核实操作', () => {
    const task = { options: { ...defaultOptions, long_horizon: true }, attempt: 2, checkpoint_revision: 12,
      progress: { milestones: [
        { id: 'a', title: '读取输入', success_criteria: '读到完整文件', status: 'completed', verification: { evidence: '完整内容已读取' } },
        { id: 'b', title: '保存输出', success_criteria: '输出内容符合要求', status: 'pending' },
      ], actions: [{ id: 'write-1', tool: 'write_file', effect: 'write', status: 'uncertain' }] } } as Task;
    render(<TaskProgressPanel task={task} />);
    expect(screen.getByText(/1\/2 个步骤已验收/)).toBeTruthy();
    expect(screen.getByText(/已保存快照 12/)).toBeTruthy();
    expect(screen.getByText('已验收')).toBeTruthy();
    expect(screen.getByText('待处理')).toBeTruthy();
    expect(screen.getByText('有操作结果待核实')).toBeTruthy();
    expect(screen.getByText('完整内容已读取')).toBeTruthy();
  });
});
