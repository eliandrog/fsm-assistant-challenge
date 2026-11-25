import { useCallback, useEffect, useRef, useState } from 'react';
import type { ChatClient } from '../api/client';
import type { BankEntry, ClientFailure, FinderOverview, RunProgress } from '../api/types';

/** How often a processing run's progress is asked for. */
export const POLL_MS = 1_000;

export interface FinderState {
  /** null until the first answer. */
  overview: FinderOverview | null;
  /** Why the bank could not be listed. */
  failure: ClientFailure | null;
  /** Why the last action (find, process, discard, delete) did not happen, in a sentence to show. */
  actionFailure: string | null;
  /** A scenario request is in flight. */
  proposing: boolean;
  /** The processing run in progress, or the last one this tab followed to its end. */
  progress: RunProgress | null;
  /** Something is in progress, here or on the server: no other action can start. */
  busy: boolean;
  /** The entry whose evidence is open in the table. */
  openId: number | null;
  setOpenId: (id: number | null) => void;
  refresh: () => void;
  propose: () => void;
  process: (id: number) => void;
  discard: (id: number) => void;
  remove: (id: number) => void;
  dismissProgress: () => void;
  /** After a decision was saved: the row shows it without another request. */
  decided: (entry: BankEntry) => void;
}

/** The rule bank as the server holds it, and the actions a reviewer takes on it. */
export function useFinder(client: ChatClient, pollMs = POLL_MS): FinderState {
  const [overview, setOverview] = useState<FinderOverview | null>(null);
  const [failure, setFailure] = useState<ClientFailure | null>(null);
  const [actionFailure, setActionFailure] = useState<string | null>(null);
  const [proposing, setProposing] = useState(false);
  const [progress, setProgress] = useState<RunProgress | null>(null);
  const [openId, setOpenId] = useState<number | null>(null);
  const alive = useRef(true);

  const refresh = useCallback(() => {
    void client.finderOverview().then((found) => {
      if (!alive.current) return;
      if (found.kind === 'ok') {
        setOverview(found.value);
        setFailure(null);
        // A page opened or reloaded during a run picks the run back up.
        const { active } = found.value;
        if (active) setProgress((prev) => (prev?.state === 'running' ? prev : active));
      } else {
        setFailure(found.failure);
      }
    });
  }, [client]);

  useEffect(() => {
    alive.current = true;
    refresh();
    return () => {
      alive.current = false;
    };
  }, [refresh]);

  const runId = progress?.state === 'running' ? progress.run_id : null;
  useEffect(() => {
    if (runId === null) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = () => {
      void client.finderProgress(runId).then((found) => {
        if (stopped || !alive.current) return;
        if (found.kind === 'ok') {
          setProgress(found.value);
          if (found.value.state === 'running') {
            timer = setTimeout(poll, pollMs);
          } else {
            if (found.value.result_entry_id !== null) setOpenId(found.value.result_entry_id);
            refresh();
          }
        } else if (found.failure.reason === 'missing') {
          // The server was restarted: the run is over, and the bank says how.
          setProgress(null);
          setActionFailure('The server no longer follows that run. It was interrupted; the scenario is still waiting.');
          refresh();
        } else {
          timer = setTimeout(poll, pollMs);
        }
      });
    };
    timer = setTimeout(poll, pollMs);
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [client, runId, pollMs, refresh]);

  const propose = useCallback(() => {
    setProposing(true);
    setActionFailure(null);
    void client.finderPropose().then((found) => {
      if (!alive.current) return;
      setProposing(false);
      if (found.kind === 'failure') setActionFailure(`No scenario was asked for. ${found.failure.message}`);
      else if (found.value.error) setActionFailure(found.value.error.message);
      refresh();
    });
  }, [client, refresh]);

  const process = useCallback(
    (id: number) => {
      setActionFailure(null);
      void client.finderProcess(id).then((found) => {
        if (!alive.current) return;
        if (found.kind === 'ok') setProgress(found.value);
        else setActionFailure(`The scenario was not processed. ${found.failure.message}`);
        refresh();
      });
    },
    [client, refresh],
  );

  const act = useCallback(
    (request: Promise<{ kind: 'ok' } | { kind: 'failure'; failure: ClientFailure }>, what: string) => {
      setActionFailure(null);
      void request.then((found) => {
        if (!alive.current) return;
        if (found.kind === 'failure') setActionFailure(`${what} ${found.failure.message}`);
        refresh();
      });
    },
    [refresh],
  );
  const discard = useCallback(
    (id: number) => act(client.finderDiscard(id), 'The scenario was not discarded.'),
    [act, client],
  );
  const remove = useCallback(
    (id: number) => {
      setOpenId((prev) => (prev === id ? null : prev));
      setProgress((prev) => (prev && prev.state !== 'running' && prev.result_entry_id === id ? null : prev));
      act(client.finderDelete(id), 'The entry was not deleted.');
    },
    [act, client],
  );

  const decided = useCallback((entry: BankEntry) => {
    setOverview((prev) =>
      prev && {
        ...prev,
        entries: prev.entries.map((row) => (row.id === entry.id ? { ...row, decision: entry.decision } : row)),
      },
    );
  }, []);
  const dismissProgress = useCallback(() => setProgress((prev) => (prev?.state === 'running' ? prev : null)), []);

  const busy = proposing || progress?.state === 'running' || overview?.running === true;
  return {
    overview, failure, actionFailure, proposing, progress, busy, openId, setOpenId,
    refresh, propose, process, discard, remove, dismissProgress, decided,
  };
}
