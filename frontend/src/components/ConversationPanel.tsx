import { Alert, Button, Card, Input, Typography } from 'antd';
import type { Task } from '../api/client';
import { active, statusLabels } from '../api/client';
import MarkdownAnswer from './MarkdownAnswer';
import VerificationNotice from './VerificationNotice';
import TaskProgressPanel from './TaskProgressPanel';

type Props = {
  tasks: Task[]; draft: string; onDraft: (value: string) => void;
  onSelect: (id: string) => void; onSubmit: () => void; submitting: boolean; disabled: boolean;
  omittedMessages: number;
  onStop: (task: Task) => void; stopping: boolean;
  onResume?: (task: Task) => void; resumingId?: string | null; summaryMode?: string;
};

export default function ConversationPanel(props: Props) {
  return <Card title="会话记录" className="conversation-card">
    {props.omittedMessages > 0 && <Alert type="info" showIcon className="history-notice"
      title={props.summaryMode === 'semantic' ? '较早历史已压缩为摘要' : props.summaryMode === 'extractive' ? '较早历史已保留为摘要片段' : '较早的部分上下文已裁剪'}
      description="任务目标和进展单独保存；引用较早细节时可补充相关信息，执行记录仍可查看。" />}
    {props.tasks.map(task => <div className="conversation-turn" key={task.id}>
      <div className="conversation-question"><strong>你</strong><Typography.Paragraph>{task.task}</Typography.Paragraph></div>
      <div className="conversation-answer"><strong>Agent</strong>
        {task.answer ? <MarkdownAnswer content={task.answer} />
          : <Typography.Paragraph>{task.error || statusLabels[task.status]}</Typography.Paragraph>}
        <VerificationNotice verification={task.verification} />
        <TaskProgressPanel task={task} />
        <div className="conversation-actions">
          <Button size="small" onClick={() => props.onSelect(task.id)}>查看此次详细执行过程</Button>
          {task.resume_available && task.id === props.tasks.at(-1)?.id && props.onResume && <Button size="small" loading={props.resumingId === task.id}
            disabled={props.disabled} onClick={() => props.onResume?.(task)}>恢复任务</Button>}
          {active(task) && <Button size="small" danger loading={props.stopping} disabled={task.status === 'stopping'}
            onClick={() => props.onStop(task)}>{task.status === 'stopping' ? '正在停止…' : '停止任务'}</Button>}
        </div>
        {task.status === 'stopping' && <p className="muted">正在等待当前调用结束</p>}
      </div>
    </div>)}
    <Input.TextArea aria-label="继续追问" value={props.draft} onChange={event => props.onDraft(event.target.value)}
      placeholder="继续提问，或说明需要如何修改刚才的结果…" autoSize={{ minRows: 3, maxRows: 9 }} maxLength={50000} />
    <div className="compose-actions"><span className="muted">追问会继承当前会话；新建会话可重新开始。</span>
      <Button type="primary" loading={props.submitting} disabled={props.disabled || !props.draft.trim()}
        onClick={props.onSubmit}>发送追问 →</Button></div>
  </Card>;
}
