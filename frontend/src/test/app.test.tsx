import { App as AntApp } from 'antd';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import App from '../App';
import { defaultOptions } from '../components/RunSettings';
import type { Session, Task } from '../api/client';

const { get, post, remove } = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), remove: vi.fn() }));
vi.mock('../api/client', async importOriginal => ({
  ...(await importOriginal<typeof import('../api/client')>()), api: { GET: get, POST: post, DELETE: remove },
}));

class Source {
  static instances: Source[] = [];
  callbacks = new Map<string, (event: MessageEvent<string>) => void>();
  onopen = null;
  onerror = null;
  constructor(public url: string) { Source.instances.push(this); }
  addEventListener(name: string, callback: (event: MessageEvent<string>) => void) { this.callbacks.set(name, callback); }
  close() {}
  emit(name: string, data: unknown) { this.callbacks.get(name)?.({ data: JSON.stringify(data) } as MessageEvent<string>); }
}

const makeTask = (): Task => ({
  id: 'task-1', session_id: 'session-1', task: '读取项目说明', options: defaultOptions as Task['options'], status: 'running',
  created_at: '2026-10-04T10:00:00Z', started_at: '2026-10-04T10:00:00Z', finished_at: null,
  answer: null, error: null, error_info: null, run_id: null, turn: 0, stop_reason: null,
  checkpoint_revision: 0, resume_available: false, attempt: 1,
});

const makeSession = (id = 'session-1'): Session => ({
  id, title: '读取项目说明', created_at: '2026-10-04T10:00:00Z', updated_at: '2026-10-04T10:00:00Z',
  options: defaultOptions as Task['options'], working_directory: null, omitted_messages: 0,
  summary_mode: 'none', compaction_runs: 0,
});

beforeEach(() => {
  history.replaceState(null, '', '/');
  Source.instances = [];
  vi.stubGlobal('EventSource', Source);
  remove.mockReset();
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
  it('已有后续问答时隐藏旧任务的恢复入口，详情中的入口不可用', async () => {
    const failed = { ...makeTask(), status: 'failed', error: '旧任务中断', resume_available: true, checkpoint_revision: 8 } as Task;
    const later = { ...makeTask(), id: 'task-2', task: '后续问题', status: 'completed', answer: '后续答案',
      created_at: '2026-10-04T11:00:00Z' } as Task;
    history.replaceState(null, '', '/?task=task-1');
    get.mockImplementation((path: string) => Promise.resolve({ data: path === '/api/capabilities'
      ? { providers: { deepseek: true }, desktop_available: true }
      : path === '/api/sessions' ? [makeSession()] : [failed, later] }));
    render(<AntApp><App /></AntApp>);
    await screen.findByText('后续答案');
    expect(screen.queryByRole('button', { name: '恢复任务' })).toBeNull();
    await userEvent.click(screen.getAllByRole('button', { name: '查看此次详细执行过程' })[0]);
    expect((await screen.findByRole('button', { name: '恢复任务' })).hasAttribute('disabled')).toBe(true);
    expect(post).not.toHaveBeenCalled();
  });

  it('恢复按钮提交快照版本，并在同一会话展示新一轮执行', async () => {
    const failed = { ...makeTask(), status: 'failed', error: '执行进程意外退出。', resume_available: true,
      checkpoint_revision: 8, options: { ...defaultOptions, long_horizon: true } } as Task;
    history.replaceState(null, '', '/?task=task-1');
    get.mockImplementation((path: string) => Promise.resolve({ data: path === '/api/capabilities'
      ? { providers: { deepseek: true }, desktop_available: true }
      : path === '/api/sessions' ? [makeSession()] : [failed] }));
    post.mockResolvedValue({ data: { ...failed, status: 'running', attempt: 2, resume_available: false, error: null } });
    render(<AntApp><App /></AntApp>);
    await userEvent.click(await screen.findByRole('button', { name: '恢复任务' }));
    expect(post).toHaveBeenCalledWith('/api/tasks/{task_id}/resume', { params: { path: { task_id: 'task-1' } },
      body: { expected_revision: 8 } });
    await screen.findByRole('button', { name: '停止任务' });
    expect(location.search).toContain('task=task-1');
    expect(Source.instances.length).toBeGreaterThan(1);
  });

  it('桌面任务提交截图记录、稳定等待与恢复预算配置', async () => {
    render(<AntApp><App /></AntApp>);
    const input = await screen.findByRole('textbox', { name: '任务描述' });
    await userEvent.click(screen.getByRole('switch', { name: '启用桌面操作' }));
    await userEvent.click(screen.getByRole('checkbox', { name: '记录操作前后截图' }));
    await userEvent.click(screen.getByText('高级设置'));
    const stability = await screen.findByRole('spinbutton', { name: '等待画面稳定上限' });
    await userEvent.clear(stability);
    await userEvent.type(stability, '1.5');
    const budget = screen.getByRole('spinbutton', { name: '无进展动作上限' });
    await userEvent.clear(budget);
    await userEvent.type(budget, '4');
    await userEvent.type(input, '保存文件');
    await userEvent.click(screen.getByRole('button', { name: '开始任务 →' }));
    expect(post).toHaveBeenCalledWith('/api/tasks', expect.objectContaining({ body: {
      task: '保存文件', options: expect.objectContaining({ desktop: true, record_desktop: true,
        stability_timeout: 1.5, desktop_recovery_limit: 4 }),
    } }));
  });

  it('提交后显示会话，停止先显示 pending，收到工作进程终态后显示已停止', async () => {
    render(<AntApp><App /></AntApp>);
    const input = await screen.findByRole('textbox', { name: '任务描述' });
    await userEvent.type(input, '读取项目说明');
    await userEvent.click(screen.getByRole('button', { name: '开始任务 →' }));
    await screen.findByRole('button', { name: '停止任务' });
    expect(screen.getByRole('button', { name: '删除会话：读取项目说明' }).hasAttribute('disabled')).toBe(true);
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
    expect(screen.getByText('用户请求停止任务。')).toBeTruthy();
  });

  it('刷新时恢复选中任务，显示最终回答并能复制为新任务', async () => {
    const task = { ...makeTask(), status: 'completed', answer: '## 项目说明\n\n项目结构整理完成。', turn: 2 } as Task;
    history.replaceState(null, '', '/?task=task-1');
    get.mockImplementation((path: string, request?: { params?: { path?: { session_id?: string } } }) => Promise.resolve({ data: path === '/api/capabilities' ? {
      providers: { deepseek: true }, desktop_available: true,
    } : path === '/api/sessions' ? [makeSession()] : request?.params?.path?.session_id === 'session-new' ? [] : [task] }));
    render(<AntApp><App /></AntApp>);
    await screen.findByText('项目结构整理完成。');
    expect(screen.getByRole('heading', { level: 2, name: '项目说明' })).toBeTruthy();
    expect(screen.queryByText('执行时间线')).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: '查看此次详细执行过程' }));
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
    await waitFor(() => expect(screen.getAllByRole('button', { name: '查看此次详细执行过程' })).toHaveLength(2));
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
    expect(screen.queryByText('执行时间线')).toBeNull();
    expect(screen.getAllByRole('button', { name: '查看此次详细执行过程' })).toHaveLength(2);
  });

  it('查看历史执行时仍实时更新当前回答，关闭详情后保持完整问答', async () => {
    const first = { ...makeTask(), status: 'completed', answer: '第一轮回答。', run_id: 'run-first' } as Task;
    const last = { ...makeTask(), id: 'task-2', task: '第二轮问题', created_at: '2026-10-04T11:00:00Z' };
    history.replaceState(null, '', '/?task=task-2');
    get.mockImplementation((path: string) => Promise.resolve({ data: path === '/api/capabilities'
      ? { providers: { deepseek: true }, desktop_available: true }
      : path === '/api/sessions' ? [makeSession()] : [first, last] }));
    render(<AntApp><App /></AntApp>);
    await screen.findByText('第一轮回答。');
    await userEvent.type(screen.getByRole('textbox', { name: '继续追问' }), '第三轮问题');
    expect(screen.getByRole('button', { name: '发送追问 →' }).hasAttribute('disabled')).toBe(true);
    expect(screen.queryByRole('dialog')).toBeNull();
    await userEvent.click(screen.getAllByRole('button', { name: '查看此次详细执行过程' })[0]);
    const dialog = await screen.findByRole('dialog', { name: '详细执行过程' });
    expect(within(dialog).getByText('run-first')).toBeTruthy();
    expect(within(dialog).getByText('执行时间线')).toBeTruthy();
    expect(location.search).toContain('task=task-2');
    const currentSource = Source.instances.find(source => source.url.includes('/task-2/'))!;
    const historySource = Source.instances.find(source => source.url.includes('/task-1/'))!;
    act(() => {
      historySource.emit('task_event', { id: 1, task_id: first.id, data: { event: 'tool_finished', tool: 'read_file' } });
      currentSource.emit('task_event', { id: 2, task_id: last.id, data: { event: 'task_updated',
        task: { ...last, status: 'completed', answer: '第二轮回答。' } } });
    });
    await screen.findByText('第二轮回答。');
    expect(within(dialog).getByText('read_file')).toBeTruthy();
    await userEvent.click(within(dialog).getByRole('button', { name: '关闭详细执行过程' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(screen.getByText('第一轮回答。')).toBeTruthy();
    expect(screen.getByText('第二轮回答。')).toBeTruthy();
    expect((screen.getByRole('textbox', { name: '继续追问' }) as HTMLTextAreaElement).value).toBe('第三轮问题');
    expect(screen.getByRole('button', { name: '发送追问 →' }).hasAttribute('disabled')).toBe(false);
  });

  it('删除需确认，取消保留记录；删除当前会话后清空页面，旧响应不会恢复记录', async () => {
    const task = { ...makeTask(), status: 'completed', answer: '删除前的回答。' } as Task;
    history.replaceState(null, '', '/?task=task-1');
    get.mockImplementation((path: string) => Promise.resolve({ data: path === '/api/capabilities'
      ? { providers: { deepseek: true }, desktop_available: true }
      : path === '/api/sessions' ? [makeSession()] : [task] }));
    remove.mockResolvedValue({ data: { id: 'session-1', deleted_tasks: 1, retained_logs: 0 } });
    render(<AntApp><App /></AntApp>);
    await screen.findByText('删除前的回答。');
    await userEvent.click(screen.getByRole('button', { name: '删除会话：读取项目说明' }));
    expect(remove).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: /取\s*消/ }));
    expect(screen.getByText('删除前的回答。')).toBeTruthy();
    await userEvent.click(screen.getByRole('button', { name: '删除会话：读取项目说明' }));
    await userEvent.click(screen.getByRole('button', { name: '确认删除' }));
    await screen.findByRole('textbox', { name: '任务描述' });
    expect(remove).toHaveBeenCalledWith('/api/sessions/{session_id}', { params: { path: { session_id: 'session-1' } } });
    expect(screen.queryByText('删除前的回答。')).toBeNull();
    expect(screen.queryByRole('button', { name: '删除会话：读取项目说明' })).toBeNull();
    expect(location.search).toBe('');
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 3100)); });
    expect(screen.queryByRole('button', { name: '删除会话：读取项目说明' })).toBeNull();
    expect(screen.queryByText('删除前的回答。')).toBeNull();
  }, 10000);

  it('删除请求失败时保留会话与回答', async () => {
    const task = { ...makeTask(), status: 'completed', answer: '保留这条回答。' } as Task;
    history.replaceState(null, '', '/?task=task-1');
    get.mockImplementation((path: string) => Promise.resolve({ data: path === '/api/capabilities'
      ? { providers: { deepseek: true }, desktop_available: true }
      : path === '/api/sessions' ? [makeSession()] : [task] }));
    remove.mockResolvedValue({ error: { detail: '删除失败，请重试。' } });
    render(<AntApp><App /></AntApp>);
    await screen.findByText('保留这条回答。');
    await userEvent.click(screen.getByRole('button', { name: '删除会话：读取项目说明' }));
    await userEvent.click(screen.getByRole('button', { name: '确认删除' }));
    await screen.findByText('删除失败，请重试。');
    expect(screen.getByText('保留这条回答。')).toBeTruthy();
    expect(screen.getByRole('button', { name: '删除会话：读取项目说明' })).toBeTruthy();
    expect(location.search).toContain('task=task-1');
  });

  it('删除其他会话保留当前会话及追问草稿', async () => {
    const current = { ...makeTask(), status: 'completed', answer: '当前回答。' } as Task;
    const other = { ...current, id: 'task-2', session_id: 'session-2', task: '旧问题', answer: '旧回答。' };
    history.replaceState(null, '', '/?task=task-1');
    get.mockImplementation((path: string, request?: { params?: { path?: { session_id?: string } } }) => Promise.resolve({
      data: path === '/api/capabilities' ? { providers: { deepseek: true }, desktop_available: true }
        : path === '/api/sessions' ? [makeSession(), { ...makeSession('session-2'), title: '旧会话' }]
          : request?.params?.path?.session_id === 'session-1' ? [current] : [current, other],
    }));
    remove.mockResolvedValue({ data: { id: 'session-2', deleted_tasks: 1, retained_logs: 0 } });
    render(<AntApp><App /></AntApp>);
    await screen.findByText('当前回答。');
    await userEvent.type(screen.getByRole('textbox', { name: '继续追问' }), '待发送的问题');
    await userEvent.click(screen.getByRole('button', { name: '删除会话：旧会话' }));
    await userEvent.click(screen.getByRole('button', { name: '确认删除' }));
    await waitFor(() => expect(screen.queryByRole('button', { name: '删除会话：旧会话' })).toBeNull());
    expect(screen.getByText('当前回答。')).toBeTruthy();
    expect((screen.getByRole('textbox', { name: '继续追问' }) as HTMLTextAreaElement).value).toBe('待发送的问题');
    expect(location.search).toContain('task=task-1');
  });
});
