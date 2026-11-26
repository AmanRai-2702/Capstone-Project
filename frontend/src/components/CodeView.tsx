import { api } from "../api/client";
import { useAsync } from "../hooks/useAsync";
import { Async } from "./ui";

/** Source lines from the repository (read through the API, which blocks path traversal). */
export function CodeView({ repoId, file, start = 1, end, highlight }: {
  repoId: string; file: string; start?: number; end?: number; highlight?: [number, number];
}) {
  const state = useAsync(() => api.source(repoId, file, start, end), [repoId, file, start, end]);
  return (
    <Async state={state}>
      {(src) => (
        <div className="code" aria-label={`Source of ${src.file}`}>
          {src.lines.map((line, i) => {
            const n = src.start + i;
            const hl = highlight && n >= highlight[0] && n <= highlight[1];
            return (
              <div key={n} className={`ln ${hl ? "hl" : ""}`}>
                <span>{n}</span><pre>{line || " "}</pre>
              </div>
            );
          })}
        </div>
      )}
    </Async>
  );
}
