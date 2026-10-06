import { Alert, Button, Card, Drawer, Tag, Typography } from 'antd';
import { active, formatTime, statusLabels } from '../api/client';
import type { Task } from '../api/client';
import { useTaskEvents } from '../hooks/useTaskEvents';
import EventTimeline from './EventTimeline';
import VerificationNotice from './VerificationNotice';
import TaskProgressPanel from './TaskProgressPanel';

type Props = {
  task?: Task; currentTaskId: string | null; currentEvents: ReturnType<typeof useTaskEvents>;
  onTask: (task: Task) => void; onClose: () => void; onCopy: (task: Task) => void;
  onStop: (task: Task) => void; stopping: boolean;
  onResume?: (task: Task) => void; resumingId?: string | null; resumeDisabled?: boolean;
};

export default function TaskExecutionDetails(props: Props) {
  // 查看历史任务时使用独立订阅，保持当前任务的问答仍然实时更新。
  const historyEvents = useTaskEvents(props.task && props.task.id !== props.currentTaskId ? props.task.id : null, props.onTask, props.task?.attempt ?? 1);
  const { events, connection } = props.task?.id === props.currentTaskId ? props.currentEvents : historyEvents;
  const task = props.task;
  return <Drawer title="详细执行过程" open={!!task} onClose={props.onClose} size="large" destroyOnHidden
    closable={{ 'aria-label': '关闭详细执行过程' }} styles={{ wrapper: { maxWidth: '100vw' } }}>
    {task && <>
      <Card className="task-detail">
        <Tag color={task.status === 'completed' ? 'success' : task.status === 'failed' ? 'error' : active(task) ? 'processing' : 'default'}>
          {statusLabels[task.status]}
        </Tag>
        <div className="task-description"><Typography.Paragraph copyable>{task.task}</Typography.Paragraph></div>
        <div className="task-facts"><span>{task.options.provider} <code>{task.options.model || 'deepseek-flash'}</code></span>
          <span>{task.options.desktop ? '桌面模式' : '文件与 Shell'}</span><span>开始于 {formatTime(task.started_at)}</span>
          <span>{task.turn || events.reduce((turn, event) => Math.max(turn, typeof event.data.turn === 'number' ? event.data.turn : 0), 0)} 轮</span>
        </div>
        {task.options.workdir && <div className="workdir">工作目录 <code>{task.options.workdir}</code></div>}
        <div className="detail-actions"><Button onClick={() => props.onCopy(task)}>复制为新任务</Button>
          {task.resume_available && props.onResume && <Button loading={props.resumingId === task.id} disabled={props.resumeDisabled}
            onClick={() => props.onResume?.(task)}>恢复任务</Button>}
          {active(task) && <Button danger loading={props.stopping} disabled={task.status === 'stopping'}
            onClick={() => props.onStop(task)}>{task.status === 'stopping' ? '正在停止…' : '停止任务'}</Button>}
        </div>
      </Card>
      {task.status === 'stopping' && <Alert className="below-card" type="info" showIcon title="正在等待当前调用结束"
        description="取消信号已发送。正在进行的模型请求或 Shell 命令返回后，Agent 会在下一个检查点停止。" />}
      {task.error && <Alert className="below-card" showIcon type={task.status === 'failed' ? 'error' : 'warning'}
        title={task.error} description={task.error_info && <span>错误代码 <code>{task.error_info.code}</code> · 阶段 {task.error_info.phase}
          {task.error_info.side_effects === 'possible' && ' · 操作可能已经产生部分影响'}</span>} />}
      <VerificationNotice verification={task.verification} />
      <TaskProgressPanel task={task} />
      <Card title="执行时间线" className="timeline-card below-card" extra={<span className="connection-label">
        {connection === 'connected' ? '● 实时更新' : connection === 'reconnecting' ? '重新连接中…' : connection === 'connecting' ? '正在连接…' : '执行记录'}</span>}>
        <EventTimeline events={events} />
      </Card>
      <Card size="small" className="diagnostic-card" title="运行信息">
        <dl><dt>任务 ID</dt><dd>{task.id}</dd><dt>运行 ID</dt><dd>{task.run_id ?? '等待生成'}</dd>
          <dt>结束原因</dt><dd>{task.stop_reason ?? '执行中'}</dd><dt>结束时间</dt><dd>{formatTime(task.finished_at)}</dd></dl>
      </Card>
    </>}
  </Drawer>;
}
