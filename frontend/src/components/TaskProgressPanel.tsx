import { Alert, Progress, Tag } from 'antd';
import type { Task } from '../api/client';

const labels: Record<string, string> = { pending: '待处理', running: '进行中', completed: '已验收', blocked: '待核实' };

export default function TaskProgressPanel({ task }: { task: Task }) {
  const progress = task.progress;
  if (!progress || (!task.options.long_horizon && !progress.milestones.length && !task.resume_available)) return null;
  const completed = progress.milestones.filter(step => step.status === 'completed').length;
  const pendingEffects = progress.actions.filter(action => !['read', 'internal'].includes(action.effect));
  return <div className="task-progress-panel">
    <strong>任务进展</strong>
    <p className="muted">第 {task.attempt ?? 1} 次执行 · {completed}/{progress.milestones.length} 个步骤已验收
      {task.checkpoint_revision ? ` · 已保存快照 ${task.checkpoint_revision}` : ''}</p>
    {!!progress.milestones.length && <Progress percent={Math.round(completed / progress.milestones.length * 100)} />}
    {progress.milestones.map(step => <div className="milestone-row" key={step.id}>
      <Tag color={step.status === 'completed' ? 'green' : step.status === 'blocked' ? 'orange' : 'default'}>{labels[step.status]}</Tag>
      <strong>{step.title}</strong><p>验收条件：{step.success_criteria}</p>
      {step.verification && <p>{step.verification.evidence}</p>}
    </div>)}
    {!!pendingEffects.length && <Alert type="warning" showIcon title="有操作结果待核实"
      description={pendingEffects.map(action => `${action.tool}（${action.id}）`).join('、') + '。恢复时会先检查当前结果。'} />}
    {progress.blocked_reason && <p>{progress.blocked_reason}</p>}
  </div>;
}
