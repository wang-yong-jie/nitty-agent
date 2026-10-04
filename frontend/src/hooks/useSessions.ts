import { useCallback, useEffect, useState } from 'react';
import { api, apiError } from '../api/client';
import type { Session } from '../api/client';

export function useSessions() {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [error, setError] = useState<string | null>(null);
  const refresh = useCallback(async () => {
    try {
      const response = await api.GET('/api/sessions');
      if (response.error) throw response.error;
      setSessions(response.data ?? []);
      setError(null);
    } catch (failure) { setError(apiError(failure)); }
  }, []);
  useEffect(() => {
    void refresh();
    const interval = window.setInterval(() => void refresh(), 3000);
    return () => window.clearInterval(interval);
  }, [refresh]);
  return { sessions, error, refresh };
}
