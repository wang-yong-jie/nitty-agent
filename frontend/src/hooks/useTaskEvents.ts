import { useEffect, useState } from 'react';
import type { Task, TaskEvent } from '../api/client';

export function useTaskEvents(taskId: string | null, onTask: (task: Task) => void, attempt = 1) {
  const [events, setEvents] = useState<TaskEvent[]>([]);
  const [connection, setConnection] = useState<'connecting' | 'connected' | 'reconnecting' | 'closed'>('closed');

  useEffect(() => {
    setEvents([]);
    if (!taskId) { setConnection('closed'); return; }
    setConnection('connecting');
    let disposed = false;
    const source = new EventSource(`/api/tasks/${encodeURIComponent(taskId)}/events/stream`);
    source.onopen = () => { if (!disposed) setConnection('connected'); };
    source.onerror = () => { if (!disposed) setConnection('reconnecting'); };
    source.addEventListener('task_event', (message: MessageEvent<string>) => {
      if (disposed) return;
      const event = JSON.parse(message.data) as TaskEvent;
      setEvents(previous => {
        if (previous.length && previous[previous.length - 1].id >= event.id) return previous;
        // 页面保留最新 1000 条，完整事件仍在 SQLite 和 JSONL 中。
        return [...previous, event].slice(-1000);
      });
      if (event.data.event === 'task_updated') onTask(event.data.task as Task);
    });
    source.addEventListener('stream_end', () => { if (!disposed) { source.close(); setConnection('closed'); } });
    return () => { disposed = true; source.close(); };
  }, [taskId, onTask, attempt]);

  return { events, connection };
}
