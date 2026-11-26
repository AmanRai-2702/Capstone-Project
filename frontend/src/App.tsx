import { Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { ChatPage } from "./pages/ChatPage";
import { ContractsPage } from "./pages/ContractsPage";
import { ExplorerPage } from "./pages/ExplorerPage";
import { GraphPage } from "./pages/GraphPage";
import { HomePage } from "./pages/HomePage";
import { ImpactPage } from "./pages/ImpactPage";
import { OverviewPage } from "./pages/OverviewPage";
import { RepoLayout } from "./pages/RepoLayout";
import { TracePage } from "./pages/TracePage";

export function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<HomePage />} />
        <Route path="repo/:id" element={<RepoLayout />}>
          <Route index element={<OverviewPage />} />
          <Route path="explorer" element={<ExplorerPage />} />
          <Route path="graph" element={<GraphPage />} />
          <Route path="trace" element={<TracePage />} />
          <Route path="impact" element={<ImpactPage />} />
          <Route path="contracts" element={<ContractsPage />} />
          <Route path="chat" element={<ChatPage />} />
        </Route>
        <Route path="*" element={<div className="empty">Page not found. <a href="/">Back to repositories</a></div>} />
      </Route>
    </Routes>
  );
}
