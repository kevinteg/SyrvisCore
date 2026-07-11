import { useState } from "react";
import { Header, type Tab } from "./components/Header";
import { OverviewPanel } from "./components/OverviewPanel";
import { ServicesPanel } from "./components/ServicesPanel";
import { RoutesPanel } from "./components/RoutesPanel";
import { LogsPanel } from "./components/LogsPanel";
import { ConfigPanel } from "./components/ConfigPanel";
import { useHealthStream } from "./lib/useHealthStream";

const TABS: Tab[] = [
  { id: "overview", label: "Overview" },
  { id: "services", label: "Services" },
  { id: "routes", label: "Routes" },
  { id: "logs", label: "Logs" },
  { id: "config", label: "Config" },
];

export default function App() {
  const [tab, setTab] = useState("overview");
  // The single owner of the live health stream; Overview renders from it.
  const { snapshot, live, isLoading, error } = useHealthStream();

  return (
    <div className="min-h-full">
      <Header overall={snapshot?.overall} live={live} tabs={TABS} tab={tab} setTab={setTab} />
      <main className="mx-auto max-w-6xl px-4 py-6">
        {tab === "overview" && (
          <OverviewPanel snapshot={snapshot} isLoading={isLoading} error={error} />
        )}
        {tab === "services" && <ServicesPanel />}
        {tab === "routes" && <RoutesPanel />}
        {tab === "logs" && <LogsPanel />}
        {tab === "config" && <ConfigPanel />}
      </main>
    </div>
  );
}
