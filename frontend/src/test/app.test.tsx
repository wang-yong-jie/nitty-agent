import { App as AntApp } from 'antd';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import App from '../App';
import { defaultOptions } from '../components/RunSettings';
import type { Session, Task } from '../api/client';

const { get, post } = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('../api/client', async importOriginal => ({
  ...(await importOriginal<typeof import('../api/client')>()), api: { GET: get, POST: post },
}));

class Source {
  static instances: Source[] = [];
  callbacks = new Map<string, (event: MessageEvent<string>) => void>();
  onopen = null;
  onerror = null;
  constructor() { Source.instances.push(this); }
  addEventListener(name: string, callback: (event: MessageEvent<string>) => void) { this.callbacks.set(name, callback); }
  close() {}
  emit(name: string, data: unknown) { this.callbacks.get(name)?.({ data: JSON.stringify(data) } as MessageEvent<string>); }
}

const makeTask = (): Task => ({
  id: 'task-1', session_id: 'session-1', task: '读取项目说明', options: defaultOptions as Task['options'], status: 'running',
  created_at: '2026-10-04T10:00:00Z', started_at: '2026-10-04T10:00:00Z', finished_at: null,
  answer: null, error: null, error_info: null, run_id: null, turn: 0, stop_reason: null,
});

const makeSession = (id = 'session-1'): Session => ({
  id, title: '读取项目说明', created_at: '2026-10-04T10:00:00Z', updated_at: '2026-10-04T10:00:00Z',
  options: defaultOptions as Task['options'], working_directory: null, omitted_messages: 0,
});

beforeEach(() => {
  history.replaceState(null, '', '/');
  Source.instances = [];
  vi.stubGlobal('EventSource', Source);
  let records: Task[] = [];
  get.mockImplementation((path: string, request?: { params?: { path?: { session_id?: string } } }) => Promise.resolve({ data: path === '/api/capabilities' ? {
    providers: { deepseek: true, openai: false, claude: false }, desktop_available: true,
    single_task: true, default_provider: 'deepseek', default_model: 'deepseek-flash',
  } : path === '/api/sessions' ? [makeSession()] : request?.params?.path?.session_id === 'session-new' ? [] : records }));
  post.mockImplementation((path: string) => {
    if (path === '/api/sessions') return Promise.resolve({ data: makeSession('session-new') });
    const task = { ...makeTask(), status: path.endsWith('/stop') ? 'stopping' : 'running' } as Task;
    records = [task];
    return Promise.resolve({ data: task });
  });
});

describe('控制台完整交互', () => {
  it('提交后进入详情，停止先显示 pending，收到工作进程终态后显示已停止', async () => {
    render(<AntApp><App /></AntApp>);
    const input = await screen.findByRole('textbox', { name: '任务描述' });
    await userEvent.type(input, '读取项目说明');
    await userEvent.click(screen.getByRole('button', { name: '开始任务 →' }));
    await screen.findByRole('button', { name: '停止任务' });
    expect(post).toHaveBeenCalledWith('/api/tasks', expect.objectContaining({ body: {
      task: '读取项目说明', options: defaultOptions,
    } }));
    expect(location.search).toContain('task=task-1');
    await userEvent.click(screen.getByRole('button', { name: '停止任务' }));
    await screen.findByRole('button', { name: /正在停止…$/ });
    expect(screen.getByText('正在等待当前调用结束')).toBeTruthy();
    const task = { ...makeTask(), status: 'cancelled', error: '用户请求停止任务。', stop_reason: 'user_cancelled' };
    act(() => Source.instances.at(-1)?.emit('task_event', { id: 3, task_id: 'task-1', data: { event: 'task_updated', task } }));
    await waitFor(() => expect(screen.queryByRole('button', { name: /正在停止…$/ })).toBeNull());
    expect(screen.getByText('此任务没有最终回答')).toBeTruthy();
  });

  it('刷新时恢复选中任务，显示最终回答并能复制为新任务', async () => {
    const task = { ...makeTask(), status: 'completed', answer: '项目结构整理完成。', turn: 2 } as Task;
    history.replaceState(null, '', '/?task=task-1');
    get.mockImplementation((path: string, request?: { params?: { path?: { session_id?: string } } }) => Promise.resolve({ data: path === '/api/capabilities' ? {
      providers: { deepseek: true }, desktop_available: true,
    } : path === '/api/sessions' ? [makeSession()] : request?.params?.path?.session_id === 'session-new' ? [] : [task] }));
    render(<AntApp><App /></AntApp>);
    await screen.findByText('项目结构整理完成。');
    await userEvent.click(screen.getByRole('button', { name: '复制为新任务' }));
    const input = await screen.findByRole('textbox', { name: '任务描述' }) as HTMLTextAreaElement;
    expect(input.value).toBe(task.task);
    expect(location.search).toBe('?session=session-new');
  });

  it('在同一会话追问，显示已有问答，新建会话后隔离历史', async () => {
    const first = { ...makeTask(), status: 'completed', answer: '项目叫海蓝。' } as Task;
    let records = [first];
    history.replaceState(null, '', '/?task=task-1');
    get.mockImplementation((path: string, request?: { params?: { path?: { session_id?: string } } }) => Promise.resolve({ data: path === '/api/capabilities'
      ? { providers: { deepseek: true }, desktop_available: true }
      : path === '/api/sessions' ? [makeSession()] : request?.params?.path?.session_id === 'session-new' ? [] : records }));
    post.mockImplementation((path: string, request: { body: { task?: string; session_id?: string } }) => {
      if (path === '/api/sessions') return Promise.resolve({ data: makeSession('session-new') });
      const task = { ...makeTask(), id: 'task-2', task: request.body.task!, created_at: '2026-10-04T11:00:00Z' };
      records = [...records, task];
      return Promise.resolve({ data: task });
    });
    render(<AntApp><App /></AntApp>);
    await screen.findByText('项目叫海蓝。');
    await userEvent.type(screen.getByRole('textbox', { name: '继续追问' }), '刚才叫什么？');
    await userEvent.click(screen.getByRole('button', { name: '发送追问 →' }));
    expect(post).toHaveBeenCalledWith('/api/tasks', { body: { task: '刚才叫什么？', session_id: 'session-1' } });
    await screen.findByRole('button', { name: '查看此次执行' });
    expect(screen.getByText('项目叫海蓝。')).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: '＋ 新建会话' }));
    await screen.findByRole('textbox', { name: '任务描述' });
    expect(screen.queryByRole('textbox', { name: '继续追问' })).toBeNull();
    expect(screen.queryByText('项目叫海蓝。')).toBeNull();
    expect(location.search).toBe('?session=session-new');
  });

  it('只有会话链接时恢复完整对话，并选中最近一次执行', async () => {
    const first = { ...makeTask(), status: 'completed', answer: '海蓝是项目名称。' } as Task;
    const last = { ...first, id: 'task-2', task: '解释项目名称', answer: '最新回答。', created_at: '2026-10-04T11:00:00Z' };
    history.replaceState(null, '', '/?session=session-1');
    get.mockImplementation((path: string) => Promise.resolve({ data: path === '/api/capabilities'
      ? { providers: { deepseek: true }, desktop_available: true }
      : path === '/api/sessions' ? [makeSession()]
        : path === '/api/sessions/{session_id}/tasks' ? [first, last] : [last] }));
    render(<AntApp><App /></AntApp>);
    await screen.findByText('最新回答。');
    expect(screen.getByText('海蓝是项目名称。')).toBeTruthy();
    expect(location.search).toContain('task=task-2');
    expect(screen.getByRole('textbox', { name: '继续追问' })).toBeTruthy();
  });
});
