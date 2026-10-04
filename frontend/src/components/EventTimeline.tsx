import { Collapse, Empty, Tag } from 'antd';
import type { TaskEvent } from '../api/client';
import { formatTime } from '../api/client';

const labels: Record<string, string> = {
  run_started: '开始执行', model_configured: '模型已配置', context_built: '准备上下文',
  model_started: '模型正在决策', model_finished: '模型响应完成', model_failed: '模型请求失败',
  model_cancelled: '模型调用中止', tool_started: '收到工具调用', tool_execution_started: '开始执行工具',
  tool_finished: '工具执行结束', tool_cancelled: '工具操作中止', run_failed: '任务执行失败',
  run_finished: 'Agent 运行结束', trace_warning: '文件日志写入异常', worker_failed: '工作进程异常',
};

export default function EventTimeline({ events }: { events: TaskEvent[] }) {
  const visible = events.filter(event => event.data.event !== 'task_updated');
  if (!visible.length) return <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="执行后，这里会显示模型决策和工具调用" />;
  return <div className="event-list">
    {events.length >= 1000 && <p className="muted">当前显示最新 1000 条事件，完整记录保存在本机。</p>}
    {[...visible].reverse().map(({ id, data }) => {
      const kind = String(data.event);
      const failed = kind.includes('failed') || data.status === 'failed' || data.status === 'rejected';
      return <div key={id} className={`event-row ${failed ? 'event-error' : ''}`}>
        <span className="event-dot" />
        <Collapse ghost items={[{ key: id, label: <div className="event-heading">
          <div><strong>{labels[kind] ?? kind}</strong>{!!data.tool && <code>{String(data.tool)}</code>}</div>
          <div className="event-meta">{typeof data.turn === 'number' && <span>第 {data.turn} 轮</span>}
            {typeof data.duration_ms === 'number' && <span>{(data.duration_ms / 1000).toFixed(2)}s</span>}
            {typeof data.status === 'string' && <Tag color={failed ? 'error' : 'default'}>{data.status}</Tag>}
            <time>{formatTime(typeof data.timestamp === 'string' ? data.timestamp : null)}</time>
          </div></div>, children: <pre className="event-json">{JSON.stringify(data, null, 2)}</pre> }]} />
      </div>;
    })}
  </div>;
}
