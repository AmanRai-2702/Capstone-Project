import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, query, request } from "./api/client";
import { fileOf, parseCitation, shortId } from "./components/ui";
import { ChatPage } from "./pages/ChatPage";
import { HomePage } from "./pages/HomePage";
import { buildTree } from "./pages/ExplorerPage";
import { RepoLayout } from "./pages/RepoLayout";

const json = (body: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
let fetchMock: ReturnType<typeof vi.fn>;
beforeEach(() => { fetchMock = vi.fn(); vi.stubGlobal("fetch", fetchMock); });
afterEach(() => { vi.unstubAllGlobals(); });

describe("helpers", () => {
  it("builds query strings and skips empty values", () => {
    expect(query({ a: "x y", b: undefined, c: "", d: 0, e: false })).toBe("?a=x+y&d=0&e=false");
    expect(query({})).toBe("");
  });
  it("shortens entity ids and parses citations", () => {
    expect(shortId("python:app/models.py:User.save")).toBe("User.save");
    expect(parseCitation("app/x.py:10-20")).toEqual({ file: "app/x.py", start: 10, end: 20 });
    expect(parseCitation("app/x.py:7")).toEqual({ file: "app/x.py", start: 7, end: 7 });
    expect(parseCitation("not a citation")).toBeNull();
    expect(fileOf("a/b.py:3")).toBe("a/b.py");
  });
  it("builds a folder tree from file paths", () => {
    const tree = buildTree([{ id: "1", name: "a/b/c.py", type: "file", language: "python", location: null }, { id: "2", name: "a/d.py", type: "file", language: "python", location: null }, { id: "3", name: "top.py", type: "file", language: "python", location: null }]);
    expect(tree.files.map((f) => f.name)).toEqual(["top.py"]);
    expect([...tree.dirs.get("a")!.dirs.keys()]).toEqual(["b"]);
    expect(tree.dirs.get("a")!.files).toHaveLength(1);
  });
});

describe("api client", () => {
  it("surfaces the server's error message", async () => {
    fetchMock.mockReturnValue(json({ detail: "No entity matches 'x'" }, 404));
    await expect(request("/api/x")).rejects.toMatchObject({ status: 404, message: "No entity matches 'x'" });
  });
  it("explains an unreachable backend", async () => {
    fetchMock.mockRejectedValue(new TypeError("fetch failed"));
    await expect(request("/api/x")).rejects.toBeInstanceOf(ApiError);
    await expect(request("/api/x")).rejects.toThrow(/Cannot reach the RepoMind API/);
  });
  it("posts JSON for questions and never sends credentials in the URL", async () => {
    fetchMock.mockReturnValue(json({ answer: "hi" }));
    await api.ask("r 1", "what?");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/repositories/r%201/ask");
    expect(JSON.parse(init.body)).toEqual({ question: "what?" });
  });
});

const REPO = { repo_id: "r1", name: "demo", origin: "/x/demo", file_count: 1, source_file_count: 1, entity_count: 3, relationship_count: 2, language_counts: { python: 1 },
  files_by_category: {}, entity_counts_by_type: {}, relationship_counts_by_type: {}, relationship_counts_by_kind: {}, unresolved_counts_by_reason: {},
  unresolved_external_references: 0, analysis_errors: [], skipped_files: {} };

describe("pages", () => {
  it("home lists analysed repositories and shows validation errors without calling the API", async () => {
    fetchMock.mockImplementation((u: string) => json(u === "/api/repositories" ? [REPO] : {}));
    render(<MemoryRouter><HomePage /></MemoryRouter>);
    expect(await screen.findByText("demo")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Analyze" }));
    expect(await screen.findByText("Enter a GitHub URL.")).toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([u]) => String(u).includes("?background"))).toHaveLength(0);
  });

  it("chat shows a grounded answer with clickable citations, the engine used and timings", async () => {
    fetchMock.mockImplementation((u: string) => {
      if (u === "/api/repositories/r1") return json(REPO);
      if (u.endsWith("/ask")) return json({ question: "q", intent: "flow", answer: "Login calls verify().", grounded: true, citations: ["app.py:3-9"], entities: [], warnings: [],
        provider: "openrouter", model: "anthropic/claude-haiku-5.5", requested_provider: "openrouter", fallback: false, fallback_reason: "", retrieval_ms: 12, generation_ms: 2400, prompt_tokens: 100, completion_tokens: 20 });
      return json({ lines: ["x"], file: "app.py", start: 1, end: 1 });
    });
    render(<MemoryRouter initialEntries={["/repo/r1"]}><Routes><Route path="repo/:id" element={<RepoLayout />}><Route index element={<ChatPage />} /></Route></Routes></MemoryRouter>);
    fireEvent.change(await screen.findByLabelText("Question"), { target: { value: "How does login work?" } });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByText("Login calls verify().")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "app.py:3-9" })).toBeInTheDocument();
    expect(screen.getByText(/haiku-5.5/)).toBeInTheDocument();
    expect(screen.getByText(/generation 2.4 s/)).toBeInTheDocument();
  });

  it("chat reports a failed request instead of failing silently", async () => {
    fetchMock.mockImplementation((u: string) => (u === "/api/repositories/r1" ? json(REPO) : json({ detail: "Question must not be empty." }, 422)));
    render(<MemoryRouter initialEntries={["/repo/r1"]}><Routes><Route path="repo/:id" element={<RepoLayout />}><Route index element={<ChatPage />} /></Route></Routes></MemoryRouter>);
    fireEvent.change(await screen.findByLabelText("Question"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Question must not be empty."));
  });
});
