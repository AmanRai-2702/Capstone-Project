import { FormEvent, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { Answer } from "../api/types";
import { CodeView } from "../components/CodeView";
import { Badge, ErrorNotice, Spinner, parseCitation } from "../components/ui";
import { useRepo } from "./RepoLayout";

type Turn = { role: "user"; text: string } | { role: "assistant"; answer: Answer };

const SUGGESTIONS = ["What is the purpose of this repository?", "Explain the architecture", "Which technologies does it use?", "What happens when the user clicks login?"];

function AnswerView({ repoId, a }: { repoId: string; a: Answer }) {
  const [open, setOpen] = useState<string | null>(null);
  const cite = open ? parseCitation(open) : null;
  return (
    <div className="msg" aria-label="Answer">
      <pre className="answer">{a.answer}</pre>
      {a.warnings.map((w) => <div key={w} className="notice small" style={{ marginTop: 8 }}>{w}</div>)}
      {a.citations.length > 0 && (
        <div style={{ marginTop: 10 }}>
          <div className="small muted">Sources (click to view the code)</div>
          <div className="chips">{a.citations.map((c) => <button key={c} className="cite" onClick={() => setOpen(open === c ? null : c)}>{c}</button>)}</div>
          {cite && <div style={{ marginTop: 8 }}><CodeView repoId={repoId} file={cite.file} start={Math.max(1, cite.start - 2)} end={cite.end + 2} highlight={[cite.start, cite.end]} /></div>}
        </div>
      )}
      <div className="row small muted" style={{ marginTop: 10 }}>
        <Badge tone={a.provider === "template" ? "" : "ok"}>{a.provider}{a.model ? ` · ${a.model.split("/").pop()}` : ""}</Badge>
        {!a.grounded && <Badge tone="warn">not grounded</Badge>}
        {a.fallback && <Badge tone="warn">fallback: {a.fallback_reason}</Badge>}
        <span>intent: {a.intent}</span><span>retrieval {Math.round(a.retrieval_ms)} ms</span>
        {a.generation_ms > 0 && <span>generation {(a.generation_ms / 1000).toFixed(1)} s</span>}
        {a.prompt_tokens > 0 && <span>{a.prompt_tokens}+{a.completion_tokens} tokens</span>}
      </div>
    </div>
  );
}

export function ChatPage() {
  const { id } = useRepo();
  const [turns, setTurns] = useState<Turn[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => { setTurns([]); setError(null); }, [id]);                 // a new repository starts a new conversation
  useEffect(() => { end.current?.scrollIntoView?.({ block: "end" }); }, [turns, busy]);

  async function send(question: string) {
    if (!question.trim() || busy) return;
    setTurns((t) => [...t, { role: "user", text: question }]); setText(""); setBusy(true); setError(null);
    try { const answer = await api.ask(id, question); setTurns((t) => [...t, { role: "assistant", answer }]); }
    catch (err) { setError(err instanceof Error ? err.message : String(err)); }
    finally { setBusy(false); }
  }
  const submit = (e: FormEvent) => { e.preventDefault(); void send(text); };

  return (
    <div className="chat">
      <div><h2 style={{ margin: 0 }}>Ask about this repository</h2>
        <div className="muted">Answers are built from facts found in the code and cite their sources. If nothing relevant is found, RepoMind says so instead of guessing.</div></div>
      {turns.length === 0 && <div className="chips">{SUGGESTIONS.map((s) => <button key={s} className="chip" onClick={() => void send(s)}>{s}</button>)}</div>}
      {turns.map((t, i) => t.role === "user" ? <div key={i} className="msg user">{t.text}</div> : <AnswerView key={i} repoId={id} a={t.answer} />)}
      {busy && <div className="row muted"><Spinner /> Thinking…</div>}
      {error && <ErrorNotice message={error} />}
      <form className="row" onSubmit={submit} style={{ position: "sticky", bottom: 0, background: "var(--bg)", padding: "8px 0" }}>
        <input type="text" className="grow" value={text} onChange={(e) => setText(e.target.value)} placeholder="Ask a question, e.g. “What breaks if I change get_user?”" aria-label="Question" maxLength={2000} />
        <button className="btn" disabled={busy || !text.trim()}>Ask</button>
      </form>
      <div ref={end} />
    </div>
  );
}
