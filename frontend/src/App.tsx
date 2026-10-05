import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, App as AntApp, Button, Card, Input, Spin, Tag } from 'antd';
import { active, api, apiError, mergeTasks, statusLabels } from './api/client';
import type { Capabilities, RunOptions, Task } from './api/client';
import RunSettings, { defaultOptions } from './components/RunSettings';
import { useTaskEvents } from './hooks/useTaskEvents';
import { useSessions } from './hooks/useSessions';
import ConversationPanel from './components/ConversationPanel';
import TaskExecutionDetails from './components/TaskExecutionDetails';
import SessionHistory from './components/SessionHistory';

function Status({ task }: { task: Task }) {
  const color = task.status === 'completed' ? 'success' : task.status === 'failed' ? 'error'
    : active(task) ? 'processing' : 'default';
  return <Tag color={color}>{statusLabels[task.status]}</Tag>;
}

export default function App() {
  const { message } = AntApp.useApp();
  const [tasks, setTasks] = useState<Task[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(() => new URLSearchParams(location.search).get('task'));
  const [sessionId, setSessionId] = useState<string | null>(() => new URLSearchParams(location.search).get('session'));
  const [followUp, setFollowUp] = useState('');
  const [detailId, setDetailId] = useState<string | null>(null);
  const { sessions, error: sessionError, refresh: refreshSessions, remove: removeSession } = useSessions();
  const deletedSessions = useRef(new Set<string>());
  const [draft, setDraft] = useState('');
  const [options, setOptions] = useState<RunOptions>(defaultOptions);
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const selected = tasks.find(task => task.id === selectedId);
  const currentSessionId = selected?.session_id ?? sessionId;
  const currentSessionRef = useRef(currentSessionId);
  currentSessionRef.current = currentSessionId;
  const conversation = tasks.filter(task => task.session_id === currentSessionId && !!currentSessionId)
    .sort((a, b) => a.created_at.localeCompare(b.created_at));
  const running = tasks.find(task => active(task));

  const mergeVisibleTasks = useCallback((previous: Task[], incoming: Task[]) => (
    mergeTasks(previous, incoming).filter(task => !deletedSessions.current.has(task.session_id ?? ''))
  ), []);
  const updateTask = useCallback((task: Task) => {
    setTasks(previous => mergeVisibleTasks(previous, [task]));
  }, [mergeVisibleTasks]);
  const currentEvents = useTaskEvents(selectedId, updateTask);

  const selectTask = useCallback((id: string | null) => {
    setSelectedId(id);
    const url = new URL(location.href);
    if (id) url.searchParams.set('task', id); else url.searchParams.delete('task');
    history.replaceState(null, '', url);
  }, []);

  const selectSession = useCallback(async (id: string) => {
    try {
      const response = await api.GET('/api/sessions/{session_id}/tasks', { params: { path: { session_id: id } } });
      if (response.error) throw response.error;
      if (deletedSessions.current.has(id)) return;
      setSessionId(id); setFollowUp(''); setDraft('');
      setDetailId(null);
      setTasks(previous => mergeVisibleTasks(previous, response.data ?? []));
      selectTask(response.data?.at(-1)?.id ?? null);
      const record = sessions.find(session => session.id === id);
      if (record?.options) setOptions(record.options);
      const url = new URL(location.href);
      url.searchParams.set('session', id);
      history.replaceState(null, '', url);
    } catch (failure) { void message.error(apiError(failure)); }
  }, [message, selectTask, sessions, mergeVisibleTasks]);

  async function deleteSession(id: string) {
    try {
      const result = await removeSession(id);
      deletedSessions.current.add(id);
      setTasks(previous => previous.filter(task => task.session_id !== id));
      setDetailId(previous => tasks.find(task => task.id === previous)?.session_id === id ? null : previous);
      if (currentSessionRef.current === id) {
        setSessionId(null); selectTask(null); setFollowUp(''); setDraft(''); setOptions(defaultOptions); setError(null);
        const url = new URL(location.href);
        url.searchParams.delete('session'); url.searchParams.delete('task');
        history.replaceState(null, '', url);
      }
      if (result?.retained_logs) void message.warning(`会话已删除，但有 ${result.retained_logs} 个运行日志文件未能清理。`);
      else void message.success('会话已删除。');
    } catch (failure) { void message.error(apiError(failure)); }
  }

  async function newSession(copy?: Task) {
    try {
      const response = await api.POST('/api/sessions', { body: { title: '新会话' } });
      if (response.error) throw response.error;
      if (!response.data) return;
      setSessionId(response.data.id); selectTask(null); setFollowUp('');
      setDetailId(null);
      setDraft(copy?.task ?? ''); setOptions(copy?.options ?? defaultOptions);
      const url = new URL(location.href);
      url.searchParams.set('session', response.data.id);
      history.replaceState(null, '', url);
      await refreshSessions();
    } catch (failure) { void message.error(apiError(failure)); }
  }

  useEffect(() => {
    if (!currentSessionId) return;
    let disposed = false;
    void api.GET('/api/sessions/{session_id}/tasks', { params: { path: { session_id: currentSessionId } } }).then(response => {
      if (disposed || deletedSessions.current.has(currentSessionId)) return;
      if (response.error) setError(apiError(response.error));
      else {
        setTasks(previous => mergeVisibleTasks(previous, response.data ?? []));
        if (!selectedId && response.data?.length) selectTask(response.data.at(-1)!.id);
      }
    }).catch(failure => { if (!disposed) setError(apiError(failure)); });
    return () => { disposed = true; };
  }, [currentSessionId, selectedId, selectTask, mergeVisibleTasks]);

  const refresh = useCallback(async () => {
    try {
      const response = await api.GET('/api/tasks');
      if (response.error) throw response.error;
      setTasks(previous => mergeVisibleTasks(previous, response.data ?? []));
      setError(null);
    } catch (failure) { setError(apiError(failure)); }
  }, [mergeVisibleTasks]);

  useEffect(() => {
    let disposed = false;
    async function load() {
      try {
        const [taskResponse, configResponse] = await Promise.all([
          api.GET('/api/tasks'), api.GET('/api/capabilities'),
        ]);
        if (taskResponse.error) throw taskResponse.error;
        if (configResponse.error) throw configResponse.error;
        if (disposed) return;
        setTasks(previous => mergeVisibleTasks(previous, taskResponse.data ?? []));
        setCapabilities(configResponse.data ?? null);
        const id = new URLSearchParams(location.search).get('task');
        if (id && !taskResponse.data?.some(task => task.id === id)) {
          const detail = await api.GET('/api/tasks/{task_id}', { params: { path: { task_id: id } } });
          if (disposed) return;
          if (detail.data) updateTask(detail.data);
          else { selectTask(null); setError('此任务不存在，已返回新建任务。'); }
        }
      } catch (failure) { if (!disposed) setError(apiError(failure)); }
      finally { if (!disposed) setLoading(false); }
    }
    void load();
    const interval = window.setInterval(() => { if (!disposed) void refresh(); }, 3000);
    return () => { disposed = true; window.clearInterval(interval); };
  }, [refresh, selectTask, updateTask, mergeVisibleTasks]);

  async function submit() {
    setSubmitting(true);
    try {
      const response = await api.POST('/api/tasks', { body: { task: draft.trim(), options,
        ...(sessionId ? { session_id: sessionId } : {}) } });
      if (response.error) throw response.error;
      if (response.data) { updateTask(response.data); selectTask(response.data.id); setSessionId(response.data.session_id ?? null); }
      await refreshSessions();
    } catch (failure) { void message.error(apiError(failure)); }
    finally { setSubmitting(false); }
  }

  async function submitFollowUp() {
    if (!currentSessionId) return;
    setSubmitting(true);
    try {
      const response = await api.POST('/api/tasks', { body: { task: followUp.trim(), session_id: currentSessionId } });
      if (response.error) throw response.error;
      if (response.data) { updateTask(response.data); selectTask(response.data.id); setFollowUp(''); }
      await refreshSessions();
    } catch (failure) { void message.error(apiError(failure)); }
    finally { setSubmitting(false); }
  }

  async function stop(task: Task) {
    setStopping(true);
    try {
      const response = await api.POST('/api/tasks/{task_id}/stop', {
        params: { path: { task_id: task.id } }, headers: { 'Content-Type': 'application/json' },
      });
      if (response.error) throw response.error;
      if (response.data) updateTask(response.data);
    } catch (failure) { void message.error(apiError(failure)); }
    finally { setStopping(false); }
  }

  const missingKey = capabilities && !capabilities.providers[options.provider ?? 'deepseek'];

  return <div className="console-shell">
    <aside className="sidebar">
      <a className="brand" href="/" onClick={event => { event.preventDefault(); void newSession(); }}>
        <span className="brand-symbol">n</span><div><strong>nitty<span>agent</span></strong><small>本地工作空间</small></div>
      </a>
      <Button block className="new-task" onClick={() => void newSession()}>＋ 新建会话</Button>
      <SessionHistory sessions={sessions} selectedId={currentSessionId} onSelect={id => void selectSession(id)}
        onDelete={deleteSession} activeSessionIds={new Set(tasks.filter(task => active(task)).flatMap(task => task.session_id ? [task.session_id] : []))} />
      <div className="sidebar-footer"><span className="online-dot" /><div>运行于本机<small>任务与日志保存在本地</small></div></div>
    </aside>

    <main className="workspace">
      <header className="workspace-header"><div className="breadcrumb">工作空间 <span>/</span> {selected ? '当前会话' : '新建任务'}</div>
        <span className="local-badge"><span className="online-dot" /> LOCAL</span></header>
      <div className="workspace-body">
        {(error || sessionError) && <Alert type="error" title={error || sessionError} showIcon action={<Button size="small" onClick={() => location.reload()}>重新连接</Button>} />}
        {loading ? <div className="loading"><Spin size="large" /><p>正在连接本地 Agent…</p></div> : <>
          <div className="page-title"><div><div className="eyebrow">AGENT CONSOLE</div><h1>{selected ? '当前会话' : '让 Agent 开始工作'}</h1>
            <p>{selected ? '查看历史问答，继续提问；每次执行的详细过程可单独打开。' : '描述你的目标，Agent 会调用工具逐步完成任务。'}</p></div>
            {selected && <Status task={selected} />}</div>

          {!selected ? <div className="compose-grid">
            <div><Card className="task-card" title="任务描述" extra={<span className="muted">一次一个任务</span>}>
              <Input.TextArea aria-label="任务描述" value={draft} onChange={event => setDraft(event.target.value)}
                placeholder={'例如：读取工作目录中的 README，整理项目结构并给出改进建议。\n\n你也可以指定需要生成的文件、完成标准或执行限制。'}
                autoSize={{ minRows: 9, maxRows: 16 }} maxLength={50000} showCount />
              <div className="compose-actions"><span className="muted">清晰的目标和完成标准有助于执行</span>
                <Button type="primary" size="large" loading={submitting} onClick={() => void submit()}
                  disabled={!draft.trim() || !!running || !options.model?.trim() || !!missingKey || !capabilities}>开始任务 →</Button></div>
            </Card>
              {running && <Alert className="below-card" type="info" showIcon title="已有任务正在执行"
                description="任务结束后可以提交下一个任务。" action={<Button onClick={() => selectTask(running.id)}>查看进度</Button>} />}
              <div className="guide-card"><h3>从一个明确的目标开始</h3><div className="guide-items">
                <div><span>01</span><strong>说明目标</strong><p>告诉 Agent 需要完成什么，以及如何判断完成。</p></div>
                <div><span>02</span><strong>选择环境</strong><p>指定模型与工作目录，按需开启桌面操作。</p></div>
                <div><span>03</span><strong>查看执行</strong><p>实时查看工具结果，遇到问题可请求停止。</p></div>
              </div></div>
            </div>
            <Card className="settings-card" title="运行设置"><RunSettings value={options} onChange={setOptions} capabilities={capabilities} />
              {missingKey && <Alert type="warning" title="模型密钥尚未配置" description="请在项目 .env 中填写对应 API_KEY，重新连接页面后继续。" />}
              <p className="settings-footnote">设置只应用于下一次提交的任务。</p>
            </Card>
          </div> : <>
            {currentSessionId && <ConversationPanel tasks={conversation} draft={followUp}
              onDraft={setFollowUp} onSelect={setDetailId} onSubmit={() => void submitFollowUp()}
              onStop={task => void stop(task)} stopping={stopping}
              omittedMessages={sessions.find(session => session.id === currentSessionId)?.omitted_messages ?? 0}
              submitting={submitting} disabled={!!running || !capabilities} />}
          </>}
        </>}
        <footer className="workspace-footer">Nitty Agent <span>本地执行 · 可追踪的每一步</span></footer>
      </div>
    </main>
    <TaskExecutionDetails task={tasks.find(task => task.id === detailId)} currentTaskId={selectedId}
      currentEvents={currentEvents} onTask={updateTask} onClose={() => setDetailId(null)}
      onCopy={task => void newSession(task)} onStop={task => void stop(task)} stopping={stopping} />
  </div>;
}
