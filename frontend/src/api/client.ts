import createClient from 'openapi-fetch';
import type { components, paths } from './schema';

export type Task = components['schemas']['TaskRecord'];
export type Session = components['schemas']['SessionRecord'];
export type RunOptions = components['schemas']['RunOptions'];
export type TaskEvent = components['schemas']['TaskEvent'];
export type Capabilities = components['schemas']['Capabilities'];

export const api = createClient<paths>();
export const desktopArtifactUrl = (taskId: string, filename: string) =>
  `/api/tasks/${encodeURIComponent(taskId)}/artifacts/${encodeURIComponent(filename)}`;

export function apiError(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === 'object' && error && 'detail' in error) {
    const detail = (error as { detail: unknown }).detail;
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) return detail.map((item: { msg?: string }) => item.msg).join('；');
  }
  return '请求失败，请确认本地服务已启动。';
}

export const active = (task?: Task | null) => !!task && ['queued', 'running', 'stopping'].includes(task.status);

export function mergeTasks(previous: Task[], incoming: Task[]): Task[] {
  const priority: Record<Task['status'], number> = {
    queued: 0, running: 1, stopping: 2, completed: 3, failed: 3, cancelled: 3, interrupted: 3,
  };
  const records = new Map(previous.map(task => [task.id, task]));
  for (const task of incoming) {
    const existing = records.get(task.id);
    // 查询响应与 SSE 可交错，重放的 queued/running 不能覆盖已确认的终态。
    if (!existing || priority[task.status] >= priority[existing.status]) records.set(task.id, task);
  }
  return [...records.values()].sort((a, b) => b.created_at.localeCompare(a.created_at));
}

export const statusLabels: Record<Task['status'], string> = {
  queued: '等待执行', running: '正在执行', stopping: '正在停止', completed: '已完成',
  failed: '执行失败', cancelled: '已停止', interrupted: '已中断',
};

export function formatTime(value?: string | null) {
  return value ? new Date(value).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }) : '—';
}
