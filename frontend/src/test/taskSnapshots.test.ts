import { describe, expect, it } from 'vitest';
import { mergeTasks } from '../api/client';
import type { Task } from '../api/client';
import { defaultOptions } from '../components/RunSettings';

describe('查询与事件竞态', () => {
  it('同一任务的新执行可进入 running，旧执行和旧快照不能覆盖恢复进度', () => {
    const old = { id: 'task', status: 'failed', attempt: 1, checkpoint_revision: 6 } as Task;
    const resumed = { ...old, status: 'running', attempt: 2, checkpoint_revision: 7 } as Task;
    expect(mergeTasks([old], [resumed])[0]).toEqual(resumed);
    expect(mergeTasks([resumed], [old])[0]).toEqual(resumed);
    expect(mergeTasks([resumed], [{ ...resumed, checkpoint_revision: 5 }])[0]).toEqual(resumed);
  });
  it('历史重放和过期查询不能将任务终态退回活动状态', () => {
    const task = { id: 'task', task: 'test', status: 'completed', answer: 'done', options: defaultOptions,
      created_at: '2026-10-04T10:00:00Z', started_at: null, finished_at: null, error: null, error_info: null,
      run_id: null, turn: 2, stop_reason: 'model_answer' } as Task;
    expect(mergeTasks([task], [{ ...task, status: 'running', answer: null }])[0]).toEqual(task);
    expect(mergeTasks([task], [{ ...task, status: 'queued', answer: null }])[0]).toEqual(task);
    expect(mergeTasks([{ ...task, status: 'stopping' }], [{ ...task, status: 'running' }])[0].status).toBe('stopping');
  });
});
