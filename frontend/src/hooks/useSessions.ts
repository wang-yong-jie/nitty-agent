import { useCallback, useEffect, useRef, useState } from 'react';
import { api, apiError } from '../api/client';
import type { Session } from '../api/client';

export function useSessions() {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [error, setError] = useState<string | null>(null);
  const deletedIds = useRef(new Set<string>());
  const refresh = useCallback(async () => {
    try {
      const response = await api.GET('/api/sessions');
      if (response.error) throw response.error;
      setSessions((response.data ?? []).filter(session => !deletedIds.current.has(session.id)));
      setError(null);
    } catch (failure) { setError(apiError(failure)); }
  }, []);
  const remove = useCallback(async (id: string) => {
    const response = await api.DELETE('/api/sessions/{session_id}', { params: { path: { session_id: id } } });
    if (response.error) throw response.error;
    deletedIds.current.add(id);
    setSessions(previous => previous.filter(session => session.id !== id));
    return response.data;
  }, []);
  useEffect(() => {
    void refresh();
    const interval = window.setInterval(() => void refresh(), 3000);
    return () => window.clearInterval(interval);
  }, [refresh]);
  return { sessions, error, refresh, remove };
}
