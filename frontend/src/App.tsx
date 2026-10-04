import { useCallback, useEffect, useState } from 'react';
import { Alert, App as AntApp, Button, Card, Empty, Input, Spin, Tag, Typography } from 'antd';
import { active, api, apiError, formatTime, mergeTasks, statusLabels } from './api/client';
import type { Capabilities, RunOptions, Task } from './api/client';
import RunSettings, { defaultOptions } from './components/RunSettings';
import EventTimeline from './components/EventTimeline';
import { useTaskEvents } from './hooks/useTaskEvents';
import { useSessions } from './hooks/useSessions';
import ConversationPanel from './components/ConversationPanel';

const { Paragraph } = Typography;

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
  const { sessions, error: sessionError, refresh: refreshSessions } = useSessions();
  const [draft, setDraft] = useState('');
  const [options, setOptions] = useState<RunOptions>(defaultOptions);
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const selected = tasks.find(task => task.id === selectedId);
  const currentSessionId = selected?.session_id ?? sessionId;
  const conversation = tasks.filter(task => task.session_id === currentSessionId && !!currentSessionId)
    .sort((a, b) => a.created_at.localeCompare(b.created_at));
  const running = tasks.find(task => active(task));

  const updateTask = useCallback((task: Task) => {
    setTasks(previous => mergeTasks(previous, [task]));
  }, []);
  const { events, connection } = useTaskEvents(selectedId, updateTask);

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
      setSessionId(id); setFollowUp(''); setDraft('');
      setTasks(previous => mergeTasks(previous, response.data ?? []));
      selectTask(response.data?.at(-1)?.id ?? null);
      const record = sessions.find(session => session.id === id);
      if (record?.options) setOptions(record.options);
      const url = new URL(location.href);
      url.searchParams.set('session', id);
      history.replaceState(null, '', url);
    } catch (failure) { void message.error(apiError(failure)); }
  }, [message, selectTask, sessions]);

  async function newSession(copy?: Task) {
    try {
      const response = await api.POST('/api/sessions', { body: { title: '新会话' } });
      if (response.error) throw response.error;
      if (!response.data) return;
      setSessionId(response.data.id); selectTask(null); setFollowUp('');
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
      if (disposed) return;
      if (response.error) setError(apiError(response.error));
      else {
        setTasks(previous => mergeTasks(previous, response.data ?? []));
        if (!selectedId && response.data?.length) selectTask(response.data.at(-1)!.id);
      }
    }).catch(failure => { if (!disposed) setError(apiError(failure)); });
    return () => { disposed = true; };
  }, [currentSessionId, selectedId, selectTask]);

  const refresh = useCallback(async () => {
    try {
      const response = await api.GET('/api/tasks');
      if (response.error) throw response.error;
      setTasks(previous => mergeTasks(previous, response.data ?? []));
      setError(null);
    } catch (failure) { setError(apiError(failure)); }
  }, []);

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
        setTasks(previous => mergeTasks(previous, taskResponse.data ?? []));
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
  }, [refresh, selectTask, updateTask]);

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
      <div className="sidebar-label">会话历史 <span>{sessions.length}</span></div>
      <nav aria-label="会话历史" className="history">
        {sessions.length ? sessions.map(session => <button key={session.id} className={`history-item ${session.id === currentSessionId ? 'selected' : ''}`}
          onClick={() => void selectSession(session.id)}>
          <span className="history-dot" />
          <div><strong>{session.title}</strong><small>{formatTime(session.updated_at)}</small></div>
        </button>) : <p className="history-empty">你的第一个任务<br />会出现在这里</p>}
      </nav>
      <div className="sidebar-footer"><span className="online-dot" /><div>运行于本机<small>任务与日志保存在本地</small></div></div>
    </aside>

    <main className="workspace">
      <header className="workspace-header"><div className="breadcrumb">工作空间 <span>/</span> {selected ? '任务详情' : '新建任务'}</div>
        <span className="local-badge"><span className="online-dot" /> LOCAL</span></header>
      <div className="workspace-body">
        {(error || sessionError) && <Alert type="error" title={error || sessionError} showIcon action={<Button size="small" onClick={() => location.reload()}>重新连接</Button>} />}
        {loading ? <div className="loading"><Spin size="large" /><p>正在连接本地 Agent…</p></div> : <>
          <div className="page-title"><div><div className="eyebrow">AGENT CONSOLE</div><h1>{selected ? '任务详情' : '让 Agent 开始工作'}</h1>
            <p>{selected ? '查看执行进度、工具调用和最终结果。' : '描述你的目标，Agent 会调用工具逐步完成任务。'}</p></div>
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
            {currentSessionId && <ConversationPanel tasks={conversation} selectedId={selected.id} draft={followUp}
              onDraft={setFollowUp} onSelect={selectTask} onSubmit={() => void submitFollowUp()}
              omittedMessages={sessions.find(session => session.id === currentSessionId)?.omitted_messages ?? 0}
              submitting={submitting} disabled={!!running || !capabilities} />}
            <Card className="task-detail"><div className="task-description"><Paragraph copyable>{selected.task}</Paragraph></div>
              <div className="task-facts"><span>{selected.options.provider} <code>{selected.options.model || 'deepseek-flash'}</code></span>
                <span>{selected.options.desktop ? '桌面模式' : '文件与 Shell'}</span><span>开始于 {formatTime(selected.started_at)}</span>
                <span>{selected.turn || events.reduce((turn, event) => Math.max(turn, typeof event.data.turn === 'number' ? event.data.turn : 0), 0)} 轮</span>
              </div>
              {selected.options.workdir && <div className="workdir">工作目录 <code>{selected.options.workdir}</code></div>}
              <div className="detail-actions"><Button onClick={() => void newSession(selected)}>复制为新任务</Button>
                {active(selected) && <Button danger loading={stopping} disabled={selected.status === 'stopping'}
                  onClick={() => void stop(selected)}>{selected.status === 'stopping' ? '正在停止…' : '停止任务'}</Button>}
              </div></Card>

            {selected.status === 'stopping' && <Alert className="below-card" type="info" showIcon title="正在等待当前调用结束"
              description="取消信号已发送。正在进行的模型请求或 Shell 命令返回后，Agent 会在下一个检查点停止。" />}
            {selected.error && <Alert className="below-card" showIcon type={selected.status === 'failed' ? 'error' : 'warning'}
              title={selected.error} description={selected.error_info && <span>错误代码 <code>{selected.error_info.code}</code> · 阶段 {selected.error_info.phase}
                {selected.error_info.side_effects === 'possible' && ' · 操作可能已经产生部分影响'}</span>} />}
            <div className="results-grid">
              <Card title="执行时间线" className="timeline-card" extra={<span className="connection-label">
                {connection === 'connected' ? '● 实时更新' : connection === 'reconnecting' ? '重新连接中…' : connection === 'connecting' ? '正在连接…' : '执行记录'}</span>}>
                <EventTimeline events={events} /></Card>
              <div className="result-column"><Card title="最终回答" className="answer-card">
                {selected.answer ? <Paragraph className="answer-text" copyable>{selected.answer}</Paragraph>
                  : active(selected) ? <div className="answer-pending"><Spin /><p>Agent 正在处理任务</p><small>最终回答将在任务完成后显示</small></div>
                    : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="此任务没有最终回答" />}
              </Card><Card size="small" className="diagnostic-card" title="运行信息">
                <dl><dt>任务 ID</dt><dd>{selected.id}</dd><dt>运行 ID</dt><dd>{selected.run_id ?? '等待生成'}</dd>
                  <dt>结束原因</dt><dd>{selected.stop_reason ?? '执行中'}</dd><dt>结束时间</dt><dd>{formatTime(selected.finished_at)}</dd></dl>
              </Card></div>
            </div>
          </>}
        </>}
        <footer className="workspace-footer">Nitty Agent <span>本地执行 · 可追踪的每一步</span></footer>
      </div>
    </main>
  </div>;
}
