import { Alert, Button, Card, Input, Typography } from 'antd';
import type { Task } from '../api/client';
import { statusLabels } from '../api/client';

type Props = {
  tasks: Task[]; selectedId: string; draft: string; onDraft: (value: string) => void;
  onSelect: (id: string) => void; onSubmit: () => void; submitting: boolean; disabled: boolean;
  omittedMessages: number;
};

export default function ConversationPanel(props: Props) {
  return <Card title="会话记录" className="conversation-card">
    {props.omittedMessages > 0 && <Alert type="info" showIcon className="history-notice"
      title="较早的部分上下文已裁剪" description="引用较早内容时，请补充相关信息；执行记录仍可查看。" />}
    {props.tasks.map(task => <div className="conversation-turn" key={task.id}>
      <div className="conversation-question"><strong>你</strong><Typography.Paragraph>{task.task}</Typography.Paragraph></div>
      <div className="conversation-answer"><strong>Agent</strong>
        {task.id === props.selectedId ? <p className="muted">{statusLabels[task.status]} · 执行详情和回答见下方</p>
          : <Typography.Paragraph>{task.answer || task.error || statusLabels[task.status]}</Typography.Paragraph>}
        {task.id !== props.selectedId && <Button size="small" onClick={() => props.onSelect(task.id)}>查看此次执行</Button>}
      </div>
    </div>)}
    <Input.TextArea aria-label="继续追问" value={props.draft} onChange={event => props.onDraft(event.target.value)}
      placeholder="继续提问，或说明需要如何修改刚才的结果…" autoSize={{ minRows: 3, maxRows: 9 }} maxLength={50000} />
    <div className="compose-actions"><span className="muted">追问会继承当前会话；新建会话可重新开始。</span>
      <Button type="primary" loading={props.submitting} disabled={props.disabled || !props.draft.trim()}
        onClick={props.onSubmit}>发送追问 →</Button></div>
  </Card>;
}
