import { useState } from 'react';
import { Button, Popconfirm } from 'antd';
import type { Session } from '../api/client';
import { formatTime } from '../api/client';

type Props = {
  sessions: Session[]; selectedId: string | null; activeSessionIds: Set<string>;
  onSelect: (id: string) => void; onDelete: (id: string) => Promise<void>;
};

export default function SessionHistory(props: Props) {
  const [deletingId, setDeletingId] = useState<string | null>(null);
  return <>
    <div className="sidebar-label">会话历史 <span>{props.sessions.length}</span></div>
    <nav aria-label="会话历史" className="history">
      {props.sessions.length ? props.sessions.map(session => {
        const blocked = props.activeSessionIds.has(session.id);
        return <div className={`history-row ${session.id === props.selectedId ? 'selected' : ''}`} key={session.id}>
          <button className="history-item" onClick={() => props.onSelect(session.id)}>
            <span className="history-dot" />
            <div><strong>{session.title}</strong><small>{formatTime(session.updated_at)}</small></div>
          </button>
          <Popconfirm title="删除这个会话？" placement="right" okText="确认删除" cancelText="取消"
            description="将删除问答、任务记录与运行日志，无法恢复。生成的文件保留。"
            okButtonProps={{ danger: true }} disabled={blocked || !!deletingId}
            onConfirm={async () => {
              setDeletingId(session.id);
              try { await props.onDelete(session.id); }
              finally { setDeletingId(null); }
            }}>
            <Button type="text" danger size="small" className="history-delete" aria-label={`删除会话：${session.title}`}
              title={blocked ? '请先停止任务并等待执行结束' : '删除会话'} disabled={blocked || !!deletingId}
              loading={deletingId === session.id}>删除</Button>
          </Popconfirm>
        </div>;
      }) : <p className="history-empty">你的第一个会话<br />会出现在这里</p>}
    </nav>
  </>;
}
