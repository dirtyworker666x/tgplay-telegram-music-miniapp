import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./index.css";
import { initTelegram, applyFullscreenLaunchFlagToDocument } from "./lib/telegram";
import { registerMiniAppIdentity } from "./lib/api";
import { AdminStatsApp } from "./components/AdminStatsApp";

const isAdminStatsRoute = typeof window !== "undefined" && window.location.pathname.startsWith("/admin/stats");

if (!isAdminStatsRoute) {
  initTelegram();
  void registerMiniAppIdentity();
  applyFullscreenLaunchFlagToDocument();
  setTimeout(applyFullscreenLaunchFlagToDocument, 250);
}

class RootErrorBoundary extends React.Component<
  { children: React.ReactNode },
  { err: Error | null }
> {
  state: { err: Error | null } = { err: null };

  static getDerivedStateFromError(err: Error) {
    return { err };
  }

  componentDidCatch() {
    const b = document.getElementById("tgplay-boot");
    if (b) b.setAttribute("data-tgplay-dismissed", "1");
    b?.remove();
  }

  render() {
    if (this.state.err) {
      return (
        <div
          className="app-scroll"
          style={{
            padding: 24,
            boxSizing: "border-box",
            fontFamily: "system-ui, -apple-system, sans-serif",
            color: "#0f172a",
            background: "#f1f5f9",
            minHeight: "100dvh",
          }}
        >
          <h1 style={{ fontSize: 18, margin: "0 0 12px" }}>TGPlay не запустился</h1>
          <p style={{ margin: "0 0 16px", lineHeight: 1.45, fontSize: 15 }}>
            Откройте приложение через бота в Telegram или включите VPN и обновите страницу. Ярлык на экране «Домой»
            иногда открывается без данных Telegram — тогда доступен только поиск как гость.
          </p>
          <button
            type="button"
            onClick={() => window.location.reload()}
            style={{
              padding: "10px 18px",
              fontSize: 15,
              borderRadius: 12,
              border: "none",
              background: "#0088cc",
              color: "#fff",
              cursor: "pointer",
            }}
          >
            Обновить
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <RootErrorBoundary>
      {isAdminStatsRoute ? (
        <AdminStatsApp />
      ) : (
        <main className="app-scroll">
          <App />
        </main>
      )}
    </RootErrorBoundary>
  </React.StrictMode>
);
