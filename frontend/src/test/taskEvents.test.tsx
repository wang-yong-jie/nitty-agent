import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useTaskEvents } from '../hooks/useTaskEvents';

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  listeners = new Map<string, (event: MessageEvent<string>) => void>();
  closed = false;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public url: string) { FakeEventSource.instances.push(this); }
  addEventListener(name: string, callback: (event: MessageEvent<string>) => void) { this.listeners.set(name, callback); }
  close() { this.closed = true; }
  emit(name: string, data: unknown) { this.listeners.get(name)?.({ data: JSON.stringify(data) } as MessageEvent<string>); }
}

beforeEach(() => { FakeEventSource.instances = []; vi.stubGlobal('EventSource', FakeEventSource); });

describe('任务事件订阅', () => {
  it('恢复同一任务时重新订阅，已关闭的旧执行连接不再更新页面', () => {
    const onTask = vi.fn();
    const { result, rerender } = renderHook(({ attempt }) => useTaskEvents('task', onTask, attempt), { initialProps: { attempt: 1 } });
    const source = FakeEventSource.instances[0];
    act(() => source.emit('stream_end', {}));
    rerender({ attempt: 2 });
    expect(FakeEventSource.instances).toHaveLength(2);
    expect(result.current.connection).toBe('connecting');
    act(() => source.emit('task_event', { id: 99, task_id: 'task', data: { event: 'task_updated', task: {} } }));
    expect(onTask).not.toHaveBeenCalled();
  });
  it('断线重连保留事件，去重重放，并以独立任务事件更新终态', () => {
    const onTask = vi.fn();
    const { result, unmount } = renderHook(() => useTaskEvents('task-1', onTask));
    const source = FakeEventSource.instances[0];
    const event = { id: 1, task_id: 'task-1', data: { event: 'model_started', turn: 1 } };
    act(() => { source.onopen?.(); source.emit('task_event', event); source.emit('task_event', event); });
    expect(result.current.events).toHaveLength(1);
    expect(result.current.connection).toBe('connected');
    act(() => source.onerror?.());
    expect(result.current.connection).toBe('reconnecting');
    const task = { id: 'task-1', status: 'completed', answer: 'done' };
    act(() => source.emit('task_event', { id: 2, task_id: 'task-1', data: { event: 'task_updated', task } }));
    expect(onTask).toHaveBeenCalledWith(task);
    act(() => source.emit('stream_end', {}));
    expect(source.closed).toBe(true);
    expect(result.current.connection).toBe('closed');
    unmount();
  });

  it('切换任务关闭旧连接并清空旧任务事件', () => {
    const onTask = vi.fn();
    const { result, rerender } = renderHook(({ id }) => useTaskEvents(id, onTask), { initialProps: { id: 'first' } });
    const source = FakeEventSource.instances[0];
    act(() => source.emit('task_event', { id: 1, task_id: 'first', data: { event: 'run_started' } }));
    expect(result.current.events).toHaveLength(1);
    rerender({ id: 'second' });
    expect(source.closed).toBe(true);
    expect(result.current.events).toHaveLength(0);
    expect(FakeEventSource.instances[1].url).toContain('second');
    act(() => source.emit('task_event', { id: 2, task_id: 'first', data: { event: 'run_finished' } }));
    expect(result.current.events).toHaveLength(0);
  });
});
