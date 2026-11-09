"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/client";
import { FINAL_STATES, type RunEvent } from "@/lib/types";

export type Connection = "loading" | "live" | "resuming" | "polling" | "idle" | "failed";

const MAX_WS_FAILURES = 3;
const POLL_MS = 2000;

interface Options { runId: string; live: boolean; wsBase: string }

/** The run's event log: the backlog over HTTP, then (for a run still going) a read-only WebSocket with a resume loop, falling back to
 *  polling /events. Events are de-duplicated by seq, so a resume that repeats some is harmless. */
export function useRunEvents({ runId, live, wsBase }: Options) {
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [connection, setConnection] = useState<Connection>("loading");
  const [loaded, setLoaded] = useState(false);
  const lastSeq = useRef(0);
  const ended = useRef(false);

  const add = useCallback((incoming: RunEvent[]) => {
    const fresh = incoming.filter((e) => e.seq > lastSeq.current);
    if (!fresh.length) return;
    lastSeq.current = fresh[fresh.length - 1]!.seq;
    if (fresh.some((e) => e.type === "run.state" && FINAL_STATES.includes(e.data.state as never))) ended.current = true;
    setEvents((prev) => prev.concat(fresh));
  }, []);

  useEffect(() => {
    let cancelled = false;
    let socket: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let failures = 0;
    const stopped = () => cancelled || ended.current;

    async function backlog() {
      for (;;) {
        const page = await api<{ events: RunEvent[]; next_after_seq: number }>(`/runs/${runId}/events?after_seq=${lastSeq.current}&limit=1000`);
        if (cancelled) return;
        add(page.events);
        if (page.events.length < 1000) return;
      }
    }

    function poll() {
      setConnection("polling");
      const tick = async () => {
        if (stopped()) { setConnection("idle"); return; }
        try { await backlog(); } catch { /* keep trying: the connection banner stays on */ }
        if (!stopped()) timer = setTimeout(tick, POLL_MS); else setConnection("idle");
      };
      void tick();
    }

    async function connect() {
      if (stopped()) { setConnection("idle"); return; }
      try {
        const t = await api<{ ticket: string; path: string }>(`/runs/${runId}/stream-ticket`, { method: "POST" });
        if (stopped()) return;
        socket = new WebSocket(`${wsBase}${t.path}?ticket=${encodeURIComponent(t.ticket)}`);
      } catch {
        return retry();
      }
      const ws = socket;
      ws.onopen = () => {
        failures = 0;
        setConnection("live");
        if (lastSeq.current > 0) ws.send(JSON.stringify({ t: "resume", after_seq: lastSeq.current })); // read-only protocol: this is the only message
      };
      ws.onmessage = (m) => {
        try {
          const msg = JSON.parse(String(m.data)) as { t: string } & Partial<RunEvent>;
          if (msg.t === "event") { const { t: _t, ...rest } = msg; void _t; add([rest as RunEvent]); }
        } catch { /* ignore a malformed frame */ }
      };
      ws.onclose = () => { if (!stopped()) retry(); else setConnection("idle"); };
      ws.onerror = () => ws.close();
    }

    function retry() {
      if (stopped()) { setConnection("idle"); return; }
      failures += 1;
      if (failures >= MAX_WS_FAILURES) return poll();
      setConnection("resuming");
      timer = setTimeout(() => void connect(), 1100 * failures); // the server allows one resume a second
    }

    void (async () => {
      try { await backlog(); } catch { if (!cancelled) setConnection("failed"); }
      if (cancelled) return;
      setLoaded(true);
      if (live && !ended.current) void connect(); else setConnection("idle");
    })();

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      socket?.close();
    };
  }, [runId, live, wsBase, add]);

  return { events, connection, loaded };
}
