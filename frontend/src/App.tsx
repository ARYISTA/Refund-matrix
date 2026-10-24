import { useCallback, useEffect, useRef, useState } from "react";
import {
  decide,
  getCase,
  getCompanyCases,
  getCompanyOverview,
  getCompanySettings,
  getCustomerCases,
  getCustomerOrders,
  getMe,
  getQueue,
  login,
  logout,
  type User,
  streamChat,
  updateCompanySettings,
} from "./api";
import type {
  Case,
  CompanyCase,
  CompanyOverview,
  CompanySettings,
  CustomerCase,
  CustomerCaseSummary,
  CustomerOrder,
  NodeName,
  PolicyResult,
  QueueItem,
  TimelineStep,
} from "./types";

type View = "customer" | "reviewer" | "detail";
type CustomerTab = "chat" | "orders";

const nodeLabels: Record<NodeName, string> = {
  triage: "Triage",
  resolution: "Resolution",
  refund_intake: "Refund intake",
  load_facts: "Load facts",
  policy_check: "Policy check",
  human_review: "Human review",
  payout: "Payout",
  responder: "Response",
};

function formatMoney(paise?: number | null) {
  return paise == null
    ? "—"
    : new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR" }).format(paise / 100);
}

function formatRelativeTime(dateString: string): string {
  try {
    const date = new Date(dateString);
    const now = new Date();
    const diffSec = Math.floor((now.getTime() - date.getTime()) / 1000);
    if (diffSec < 60) return "Just now";
    if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`;
    if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h ago`;
    return `${Math.floor(diffSec / 86400)}d ago`;
  } catch {
    return "";
  }
}

function App() {
  const [path, setPath] = useState(() => window.location.pathname);
  const [view, setView] = useState<View>("customer");
  const [theme, setTheme] = useState<"light" | "dark">(
    () => (localStorage.getItem("theme") as "light" | "dark") ?? "light",
  );
  const [caseId, setCaseId] = useState<string | null>(() => localStorage.getItem("thread_id"));
  const [caseData, setCaseData] = useState<Case | CustomerCase | null>(null);
  const [queue, setQueue] = useState<QueueItem[]>([]);
  const [overview, setOverview] = useState<CompanyOverview | null>(null);
  const [companyCases, setCompanyCases] = useState<CompanyCase[]>([]);
  const [companySettings, setCompanySettings] = useState<CompanySettings | null>(null);
  const [customerOrders, setCustomerOrders] = useState<CustomerOrder[]>([]);
  const [customerCases, setCustomerCases] = useState<CustomerCaseSummary[]>([]);
  const [customerTab, setCustomerTab] = useState<CustomerTab>("chat");
  const [streamingText, setStreamingText] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [user, setUser] = useState<User | null>(null);
  const [authChecked, setAuthChecked] = useState(false);

  const navigate = (nextPath: string) => {
    window.history.pushState({}, "", nextPath);
    setPath(nextPath);
  };

  const refreshQueue = () =>
    getQueue()
      .then(setQueue)
      .catch((e: Error) => setError(e.message));

  const refreshCompanyCases = (search = searchQuery, status = statusFilter) =>
    getCompanyCases(search, status)
      .then(setCompanyCases)
      .catch((e: Error) => setError(e.message));

  const refreshCustomerData = () => {
    getCustomerOrders()
      .then(setCustomerOrders)
      .catch(() => {});
    getCustomerCases()
      .then(setCustomerCases)
      .catch(() => {});
  };

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("theme", theme);
  }, [theme]);

  const refreshCase = useCallback(async () => {
    if (!caseId) return;
    try {
      const data = await getCase(caseId);
      setCaseData(data);
    } catch {
      localStorage.removeItem("thread_id");
      setCaseId(null);
      setCaseData(null);
    }
  }, [caseId]);

  useEffect(() => {
    if (!caseId) return;
    refreshCase();
    const interval = window.setInterval(refreshCase, 3000);
    return () => window.clearInterval(interval);
  }, [caseId, refreshCase]);

  useEffect(() => {
    getMe()
      .then(setUser)
      .catch(() => setUser(null))
      .finally(() => setAuthChecked(true));
  }, []);

  useEffect(() => {
    const onPopState = () => setPath(window.location.pathname);
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  useEffect(() => {
    if (!authChecked) return;
    if (!user) {
      if (path !== "/login") navigate("/login");
      return;
    }
    const home = user.role === "manager" ? "/company" : "/app";
    if (
      path === "/" ||
      path === "/login" ||
      (user.role === "customer" && path === "/company") ||
      (user.role === "manager" && path === "/app")
    ) {
      navigate(home);
    }
  }, [authChecked, path, user]);

  useEffect(() => {
    if (user?.role === "manager") {
      setView("reviewer");
      refreshQueue();
      Promise.all([getCompanyOverview(), getCompanyCases(), getCompanySettings()])
        .then(([nextOverview, nextCases, nextSettings]) => {
          setOverview(nextOverview);
          setCompanyCases(nextCases);
          setCompanySettings(nextSettings);
        })
        .catch((e: Error) => setError(e.message));
    } else if (user) {
      setView("customer");
      refreshCustomerData();
      const savedCaseId = localStorage.getItem("thread_id");
      if (savedCaseId) {
        setCaseId(savedCaseId);
        getCase(savedCaseId)
          .then(setCaseData)
          .catch(() => {
            localStorage.removeItem("thread_id");
            setCaseId(null);
          });
      }
    }
  }, [user]);

  // Start fresh conversation (prevents output leakage)
  const startNewChat = () => {
    setCaseId(null);
    setCaseData(null);
    setStreamingText("");
    setError("");
    localStorage.removeItem("thread_id");
    setCustomerTab("chat");
  };

  // Open existing case (loads its own isolated history & pipeline)
  const openCustomerCase = (id: string) => {
    setCaseId(id);
    localStorage.setItem("thread_id", id);
    setCustomerTab("chat");
    setStreamingText("");
    setError("");
    getCase(id)
      .then(setCaseData)
      .catch((e: Error) => setError(e.message));
  };

  const send = async (message: string) => {
    setError("");
    setLoading(true);
    setStreamingText("");

    const userMessage = { role: "customer" as const, content: message };
    setCaseData((current) => {
      const prev = current?.messages ?? [];
      const updatedMessages = [...prev, userMessage];
      if (current) {
        return { ...current, messages: updatedMessages };
      }
      return {
        id: "temp",
        thread_id: "temp",
        status: "under_review",
        messages: updatedMessages,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      };
    });

    let accumulatedTokens = "";
    try {
      await streamChat(message, caseId, (type, data) => {
        if (type === "case" && data.case_id) {
          const id = String(data.case_id);
          setCaseId(id);
          localStorage.setItem("thread_id", id);
        }
        if (type === "token" && data.text) {
          accumulatedTokens += String(data.text);
          setStreamingText(accumulatedTokens);
        }
        if (type === "policy" && data.result) {
          const pr = data.result as PolicyResult;
          setCaseData((curr) => (curr ? { ...curr, policy_result: pr } : curr));
        }
        if (type === "interrupt") {
          accumulatedTokens =
            "Your refund request has been evaluated and forwarded for manager review. You can monitor the real-time policy checks in the audit panel on the right.";
          setStreamingText(accumulatedTokens);
        }
        if (type === "done" && data.case_id) {
          const finalCaseId = String(data.case_id);
          setCaseId(finalCaseId);
          localStorage.setItem("thread_id", finalCaseId);
          getCase(finalCaseId)
            .then((updated) => {
              setCaseData(updated);
              refreshCustomerData();
            })
            .catch(() => {});
        }
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : "The conversation could not be completed.");
    } finally {
      setLoading(false);
      setStreamingText("");
    }
  };

  const handleOrderRefundClick = (order: CustomerOrder) => {
    startNewChat();
    send(`I would like to request a refund for order ${order.order_id}. The product was damaged.`);
  };

  const openQueueCase = (id: string) => {
    setCaseId(id);
    setView("detail");
  };

  const handleLogout = () => {
    logout()
      .then(() => {
        setUser(null);
        navigate("/login");
      })
      .catch((e: Error) => setError(e.message));
  };

  if (!authChecked) {
    return <div className="auth-loading">Checking your session...</div>;
  }

  if (!user) {
    return (
      <Login
        onLogin={(nextUser) => {
          setUser(nextUser);
          navigate(nextUser.role === "manager" ? "/company" : "/app");
        }}
      />
    );
  }

  const manager = user.role === "manager";

  return (
    <div className="claude-shell">
      {/* =========================================================
          LEFT SIDEBAR PANEL (CLAUDE STYLE)
         ========================================================= */}
      <aside className="claude-sidebar">
        {/* Brand */}
        <div className="sidebar-brand">
          <div className="brand-gem">RM</div>
          <div className="brand-text">
            <span className="brand-title">Refund Matrix</span>
            <span className="brand-tag">Autonomous Intelligence</span>
          </div>
        </div>

        {/* Start New Chat Action */}
        {!manager && (
          <button className="btn-new-inquiry" onClick={startNewChat}>
            <span className="plus-icon">+</span>
            <span>New Refund Inquiry</span>
          </button>
        )}

        {/* Sidebar Nav */}
        <nav className="sidebar-nav">
          {!manager ? (
            <>
              <button
                className={`sidebar-nav-item ${customerTab === "chat" && !caseId ? "active" : ""}`}
                onClick={startNewChat}
              >
                <span>💬</span>
                <span>Refund Assistant</span>
              </button>
              <button
                className={`sidebar-nav-item ${customerTab === "orders" ? "active" : ""}`}
                onClick={() => setCustomerTab("orders")}
              >
                <span>📦</span>
                <span>My Orders</span>
                <span className="nav-count-badge">{customerOrders.length}</span>
              </button>
            </>
          ) : (
            <>
              <button
                className={`sidebar-nav-item ${view === "reviewer" ? "active" : ""}`}
                onClick={() => {
                  setView("reviewer");
                  navigate("/company");
                  refreshQueue();
                  refreshCompanyCases();
                }}
              >
                <span>📊</span>
                <span>Overview & Queue</span>
                <span className="nav-count-badge">{queue.length}</span>
              </button>
            </>
          )}
        </nav>

        {/* Recent Cases History List (Isolated per case) */}
        {!manager && (
          <div className="sidebar-history">
            <div className="sidebar-section-title">RECENT CONVERSATIONS</div>
            <div className="sidebar-history-list">
              {customerCases.length === 0 ? (
                <div className="history-empty-text">No previous inquiries</div>
              ) : (
                customerCases.map((c) => (
                  <button
                    key={c.case_id}
                    className={`history-item-btn ${caseId === c.case_id ? "active" : ""}`}
                    onClick={() => openCustomerCase(c.case_id)}
                    title={`Click to view case ${c.case_id}`}
                  >
                    <div className="history-item-top">
                      <span className="history-item-title">
                        {c.order_id ? `Order ${c.order_id}` : "General Inquiry"}
                      </span>
                      <span className={`status-dot ${c.status}`} />
                    </div>
                    <div className="history-item-meta">
                      <span>{c.amount_paise ? formatMoney(c.amount_paise) : "Inquiry"}</span>
                      <span>{formatRelativeTime(c.created_at)}</span>
                    </div>
                  </button>
                ))
              )}
            </div>
          </div>
        )}

        {/* Sidebar Footer User Info & Theme */}
        <div className="sidebar-footer">
          <div className="sidebar-user-card">
            <div className="user-avatar">{user.email[0].toUpperCase()}</div>
            <div className="user-info-text">
              <span className="user-email-text" title={user.email}>{user.email}</span>
              <span className="user-role-text">{manager ? "Manager / Reviewer" : "Verified Customer"}</span>
            </div>
          </div>
          <div className="sidebar-actions-row">
            <button
              className="btn-sidebar-sm"
              onClick={() => setTheme(theme === "light" ? "dark" : "light")}
            >
              {theme === "light" ? "🌙 Dark" : "☀️ Light"}
            </button>
            <button className="btn-sidebar-sm" onClick={handleLogout}>
              Sign out
            </button>
          </div>
        </div>
      </aside>

      {/* =========================================================
          MAIN WORKSPACE
         ========================================================= */}
      <div className="claude-workspace">
        {error && (
          <div className="error-banner" role="alert">
            <span>{error}</span>
            <button onClick={() => setError("")}>✕</button>
          </div>
        )}

        {manager ? (
          view === "detail" && caseData && "awaiting_approval" in caseData ? (
            <Detail
              caseData={caseData}
              onBack={() => setView("reviewer")}
              onDecision={(action) =>
                decide(caseData.id, action)
                  .then(() => getCase(caseData.id).then(setCaseData))
                  .catch((e: Error) => setError(e.message))
              }
            />
          ) : (
            <ManagerPortal
              overview={overview}
              cases={companyCases}
              settings={companySettings}
              items={queue}
              onOpen={openQueueCase}
              loading={loading}
              onSettingsSaved={setCompanySettings}
              searchQuery={searchQuery}
              statusFilter={statusFilter}
              onSearchChange={(q) => {
                setSearchQuery(q);
                refreshCompanyCases(q, statusFilter);
              }}
              onStatusFilterChange={(s) => {
                setStatusFilter(s);
                refreshCompanyCases(searchQuery, s);
              }}
            />
          )
        ) : customerTab === "orders" ? (
          <OrdersView
            orders={customerOrders}
            onOrderRefund={handleOrderRefundClick}
          />
        ) : (
          <>
            {/* Center: Conversational Chat */}
            <ClaudeChat
              caseData={caseData}
              onSend={send}
              loading={loading}
              streamingText={streamingText}
              onNewChat={startNewChat}
            />

            {/* Right: Pipeline & Process Audit (Bound strictly to active case) */}
            <ClaudePipelinePanel
              caseData={caseData}
              orders={customerOrders}
              onDecision={(action) =>
                caseData &&
                decide(caseData.id, action)
                  .then(() => getCase(caseData.id).then(setCaseData))
                  .catch((e: Error) => setError(e.message))
              }
            />
          </>
        )}
      </div>
    </div>
  );
}

function Login({ onLogin }: { onLogin: (user: User) => void }) {
  const [email, setEmail] = useState("asha.verma@example.com");
  const [password, setPassword] = useState("demo-password");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  return (
    <main className="login-page">
      <div className="login-box">
        <div className="wordmark">
          <span className="brand-gem" style={{ width: "28px", height: "28px", display: "inline-grid", marginRight: "10px" }}>RM</span>
          Refund Matrix
        </div>
        <div className="eyebrow">Enterprise Autonomous Resolution</div>
        <h1>Sign In</h1>
        <p className="lede">Access autonomous claims triage, policy evaluation, and payout gateway.</p>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            setError("");
            setLoading(true);
            login(email, password)
              .then(onLogin)
              .catch((e: Error) => setError(e.message))
              .finally(() => setLoading(false));
          }}
        >
          <label>
            Email address
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
            />
          </label>
          <label>
            Password
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </label>
          {error && <p className="form-error" role="alert">{error}</p>}
          <button className="login-button" disabled={loading}>
            {loading ? "Signing in..." : "Continue →"}
          </button>
        </form>
        <div className="login-hint">
          <strong>Demo credentials:</strong>
          <div>Customer: <code>asha.verma@example.com</code> · <code>demo-password</code></div>
          <div>Reviewer: <code>reviewer@example.com</code> · <code>demo-password</code></div>
        </div>
      </div>
    </main>
  );
}

/* =========================================================
   CENTER CHAT COMPONENT (CHATGPT / CLAUDE STYLE)
   ========================================================= */
function ClaudeChat({
  caseData,
  onSend,
  loading,
  streamingText,
  onNewChat,
}: {
  caseData: Case | CustomerCase | null;
  onSend: (message: string) => void;
  loading: boolean;
  streamingText: string;
  onNewChat: () => void;
}) {
  const [message, setMessage] = useState("");
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const messages = caseData?.messages ?? [];
  const activeOrderId = caseData && "order_id" in caseData ? caseData.order_id : null;

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages.length, streamingText, loading]);

  const handleSubmit = (text: string) => {
    const trimmed = text.trim();
    if (trimmed && !loading) {
      onSend(trimmed);
      setMessage("");
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit(message);
    }
  };

  return (
    <section className="claude-chat-col">
      {/* Top Header */}
      <div className="chat-top-header">
        <div className="chat-title-group">
          <span className="chat-status-dot" />
          <span className="chat-title-text">
            {activeOrderId ? `Order ${activeOrderId} Refund Inquiry` : "Refund Assistant"}
          </span>
          {caseData?.id && caseData.id !== "temp" && (
            <span className="chat-ref-id">Case #{caseData.id.slice(0, 8)}</span>
          )}
        </div>
        {messages.length > 0 && (
          <button className="btn-sidebar-sm" onClick={onNewChat} title="Start clean conversation">
            + New Chat
          </button>
        )}
      </div>

      {/* Messages Scroll Area */}
      <div className="claude-messages-container" aria-live="polite">
        {messages.length === 0 && !loading && (
          <div className="chat-welcome-box">
            <div className="welcome-gem-icon">☕</div>
            <h1>How can we assist with your order?</h1>
            <p className="lede">
              Ask policy questions, check order refund eligibility, or trigger autonomous refund processing.
            </p>
            <div className="welcome-pills-list">
              <button
                className="welcome-pill-btn"
                onClick={() => handleSubmit("I would like to request a refund for order ORD-3001. The kettle arrived damaged.")}
              >
                <span>📦 Request refund for damaged kettle (ORD-3001)</span>
                <span>→</span>
              </button>
              <button
                className="welcome-pill-btn"
                onClick={() => handleSubmit("What is your refund policy window and return criteria?")}
              >
                <span>⏱️ What is the refund policy window?</span>
                <span>→</span>
              </button>
              <button
                className="welcome-pill-btn"
                onClick={() => handleSubmit("Can I check the delivery status of order ORD-3002?")}
              >
                <span>🔍 Look up delivery and payment status for ORD-3002</span>
                <span>→</span>
              </button>
            </div>
          </div>
        )}

        {/* Message Items */}
        {messages.map((item, index) => (
          <div className={`chat-message-row ${item.role}`} key={`${index}-${item.role}`}>
            <div className="chat-message-avatar">
              {item.role === "customer" ? "👤" : "✨"}
            </div>
            <div className="chat-message-body-wrapper">
              <span className="chat-sender-name">
                {item.role === "customer" ? "You" : "Refund Matrix AI"}
              </span>
              <div className="chat-message-card">
                {item.content}
              </div>
            </div>
          </div>
        ))}

        {/* Streaming Token in Live Bubble */}
        {streamingText && (
          <div className="chat-message-row assistant">
            <div className="chat-message-avatar">✨</div>
            <div className="chat-message-body-wrapper">
              <span className="chat-sender-name">Refund Matrix AI</span>
              <div className="chat-message-card">
                {streamingText}
                <span className="cursor-blink">▍</span>
              </div>
            </div>
          </div>
        )}

        {/* Thinking Indicator */}
        {loading && !streamingText && (
          <div className="chat-message-row assistant">
            <div className="chat-message-avatar">✨</div>
            <div className="chat-message-body-wrapper">
              <span className="chat-sender-name">Refund Matrix AI</span>
              <div className="thinking-dots-bubble">
                <span className="thinking-dot" />
                <span className="thinking-dot" />
                <span className="thinking-dot" />
              </div>
            </div>
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>

      {/* Floating Bottom Composer */}
      <div className="claude-composer-wrapper">
        <form
          className="claude-composer"
          onSubmit={(e) => {
            e.preventDefault();
            handleSubmit(message);
          }}
        >
          <textarea
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            onKeyDown={handleKeyDown}
            maxLength={2000}
            placeholder="Ask a question or enter order ID (e.g., ORD-3001)... Press Enter ↵ to send"
            aria-label="Message input"
            disabled={loading}
          />
          <div className="claude-composer-footer">
            <span>{message.length}/2000 · Press Enter ↵ to send (Shift+Enter for newline)</span>
            <button className="btn-claude-send" disabled={loading || !message.trim()}>
              {loading ? "Thinking..." : "Send"}
            </button>
          </div>
        </form>
      </div>
    </section>
  );
}

/* =========================================================
   RIGHT PIPELINE & PROCESS PANEL (ISOLATED PER CONVERSATION)
   ========================================================= */
function ClaudePipelinePanel({
  caseData,
  orders = [],
  onDecision,
}: {
  caseData: Case | CustomerCase | null;
  orders?: CustomerOrder[];
  onDecision: (action: "approve" | "deny") => void;
}) {
  const activeOrderId = caseData && "order_id" in caseData && caseData.order_id ? caseData.order_id : null;
  const linkedOrder = activeOrderId ? orders.find((o) => o.order_id === activeOrderId) : null;
  const policyResult = caseData?.policy_result;
  const hasTimeline = caseData && "timeline" in caseData && caseData.timeline && caseData.timeline.length > 0;

  return (
    <aside className="claude-pipeline-col">
      <div className="pipeline-header">
        <h2>Pipeline & Audit</h2>
        {caseData && <StatusBadge status={caseData.status} />}
      </div>

      {!caseData ? (
        <div className="skeleton-panel">
          <div className="skeleton-line" />
          <div className="skeleton-line short" />
          <div className="skeleton-block" />
          <p style={{ color: "var(--muted)", fontSize: "0.85rem", marginTop: "14px", textAlign: "center", lineHeight: "1.5" }}>
            Select an inquiry from the sidebar or start typing in chat. The autonomous pipeline will evaluate order facts and deterministic policy checks in real-time.
          </p>
        </div>
      ) : (
        <>
          {/* 1. Linked Order Context Card */}
          <div className="active-order-card">
            <div className="active-order-header">
              <span className="active-order-id">
                {activeOrderId ? `Order ${activeOrderId}` : "Order Context"}
              </span>
              {linkedOrder && <span className={`status ${linkedOrder.payment_status}`}>{linkedOrder.payment_status}</span>}
            </div>

            {linkedOrder ? (
              <>
                <div>
                  {linkedOrder.items.map((item) => (
                    <div className="order-item-row" key={item.item_id}>
                      <span>{item.name} × {item.quantity}</span>
                      <strong style={{ color: "var(--text)" }}>{formatMoney(item.unit_price_paise * item.quantity)}</strong>
                    </div>
                  ))}
                </div>
                <div className="order-total-footer">
                  <span>Total Amount:</span>
                  <strong style={{ color: "var(--text)" }}>{formatMoney(linkedOrder.total_paise)}</strong>
                </div>
              </>
            ) : activeOrderId ? (
              <div className="order-total-footer">
                <span>Referenced Order:</span>
                <strong>{activeOrderId}</strong>
              </div>
            ) : (
              <p style={{ color: "var(--muted)", fontSize: "0.8rem", margin: "4px 0 0" }}>
                No specific order detected in this turn. Mention an order ID like <code>ORD-3001</code> to pull live order records.
              </p>
            )}
          </div>

          {/* 2. Autonomous Pipeline 4-Stage Vertical Track */}
          <div className="pipeline-track">
            <div className={`pipeline-stage-card ${caseData.messages.length > 0 ? "completed" : ""}`}>
              <div className="stage-icon-badge">✓</div>
              <div className="stage-text-block">
                <strong>1. Intent & Triage</strong>
                <span>Classified inquiry intent</span>
              </div>
            </div>

            <div className={`pipeline-stage-card ${activeOrderId ? "completed" : caseData.status === "under_review" ? "active" : ""}`}>
              <div className="stage-icon-badge">{activeOrderId ? "✓" : "⏳"}</div>
              <div className="stage-text-block">
                <strong>2. Fact Extraction</strong>
                <span>{activeOrderId ? `Loaded facts for ${activeOrderId}` : "Waiting for order context"}</span>
              </div>
            </div>

            <div className={`pipeline-stage-card ${policyResult ? "completed" : caseData.status === "under_review" ? "active" : ""}`}>
              <div className="stage-icon-badge">{policyResult ? "✓" : "⏳"}</div>
              <div className="stage-text-block">
                <strong>3. Deterministic Policy Check</strong>
                <span>{policyResult ? `Evaluated ${policyResult.rules.length} rules (${policyResult.decision})` : "Policy check runs on intake"}</span>
              </div>
            </div>

            <div className={`pipeline-stage-card ${caseData.status === "processed" || caseData.status === "paid" ? "completed" : caseData.status === "not_eligible" || caseData.status === "rejected" ? "completed" : caseData.status === "under_review" ? "active" : ""}`}>
              <div className="stage-icon-badge">
                {caseData.status === "processed" || caseData.status === "paid" ? "₹" : caseData.status === "not_eligible" || caseData.status === "rejected" ? "✕" : "⏳"}
              </div>
              <div className="stage-text-block">
                <strong>4. Outcome & Settlement</strong>
                <span>
                  {caseData.status === "processed" || caseData.status === "paid"
                    ? "Autonomous Payout Dispatched"
                    : caseData.status === "under_review"
                    ? "Pending Manager Review"
                    : caseData.status.replace("_", " ")}
                </span>
              </div>
            </div>
          </div>

          {/* 3. Deterministic Policy Checklist */}
          {policyResult && (
            <div className="policy-card">
              <div className="policy-card-title-row">
                <h3 style={{ margin: 0, fontSize: "0.95rem" }}>Deterministic Policy Rules</h3>
                <StatusBadge status={policyResult.decision.toLowerCase()} />
              </div>
              {policyResult.rules.map((rule) => (
                <div className="policy-rule-row" key={rule.rule}>
                  <span className={`rule-check-pill ${rule.passed ? "pass" : "fail"}`}>
                    {rule.passed ? "PASS" : "FAIL"}
                  </span>
                  <div className="rule-detail-block">
                    <strong>{rule.rule.replaceAll("_", " ")}</strong>
                    <p>{rule.reason}</p>
                  </div>
                </div>
              ))}
              <div className="policy-meta-footer">
                <span>Policy v{policyResult.policy_version} · Verified amount: {formatMoney(policyResult.amount_paise)}</span>
              </div>
            </div>
          )}

          {/* 4. Human Review Approval Panel */}
          {"awaiting_approval" in caseData && caseData.awaiting_approval && (
            <ApprovalPanel onDecision={onDecision} />
          )}

          {/* 5. Detailed Timeline */}
          {hasTimeline && caseData.timeline && caseData.timeline.length > 0 && (
            <div style={{ marginTop: "16px" }}>
              <div className="eyebrow">LangGraph Node Trail</div>
              <Timeline steps={caseData.timeline} />
            </div>
          )}

          {/* 6. Dispatched Payout */}
          {"payout" in caseData && caseData.payout && (
            <div className="active-order-card" style={{ marginTop: "16px" }}>
              <div className="active-order-header">
                <span className="active-order-id">Dispatched Payout</span>
                <StatusBadge status="paid" />
              </div>
              <div className="order-item-row">
                <span>UTR Number:</span>
                <code>{caseData.payout.utr}</code>
              </div>
              <div className="order-item-row">
                <span>Amount:</span>
                <strong>{formatMoney(caseData.payout.amount_paise)}</strong>
              </div>
              <div className="order-item-row">
                <span>Masked Account:</span>
                <span>{caseData.payout.account_masked}</span>
              </div>
            </div>
          )}
        </>
      )}
    </aside>
  );
}

/* =========================================================
   ORDERS PAGE VIEW
   ========================================================= */
function OrdersView({
  orders,
  onOrderRefund,
}: {
  orders: CustomerOrder[];
  onOrderRefund: (order: CustomerOrder) => void;
}) {
  return (
    <div className="orders-page-container">
      <div className="eyebrow">Purchases & Deliveries</div>
      <h1>My Orders</h1>
      <p className="lede">
        Select any delivered order below to initiate an autonomous refund claim.
      </p>

      <div className="orders-grid">
        {orders.map((o) => (
          <div className="order-card" key={o.order_id}>
            <div className="order-card-header">
              <span className="order-card-id">{o.order_id}</span>
              <span className="order-card-amount">{formatMoney(o.total_paise)}</span>
            </div>
            <div className="order-card-items">
              {o.items.map((i) => (
                <div key={i.item_id}>
                  {i.name} (×{i.quantity})
                </div>
              ))}
            </div>
            <div className="order-card-footer">
              <span className={`status ${o.payment_status}`}>{o.payment_status}</span>
              {o.can_refund ? (
                <button
                  className="btn-refund-order"
                  onClick={() => onOrderRefund(o)}
                >
                  Request Refund →
                </button>
              ) : (
                <small style={{ color: "var(--muted)" }}>Ineligible</small>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

/* =========================================================
   MANAGER / REVIEWER PORTAL
   ========================================================= */
function ManagerPortal({
  overview,
  cases,
  settings,
  items,
  onOpen,
  loading,
  onSettingsSaved,
  searchQuery,
  statusFilter,
  onSearchChange,
  onStatusFilterChange,
}: {
  overview: CompanyOverview | null;
  cases: CompanyCase[];
  settings: CompanySettings | null;
  items: QueueItem[];
  onOpen: (id: string) => void;
  loading: boolean;
  onSettingsSaved: (settings: CompanySettings) => void;
  searchQuery: string;
  statusFilter: string;
  onSearchChange: (query: string) => void;
  onStatusFilterChange: (status: string) => void;
}) {
  const [rupees, setRupees] = useState(
    settings ? String(settings.threshold_paise / 100) : "",
  );
  const [saving, setSaving] = useState(false);
  const [saveSuccess, setSaveSuccess] = useState(false);
  const [saveError, setSaveError] = useState("");

  useEffect(() => {
    if (settings) setRupees(String(settings.threshold_paise / 100));
  }, [settings]);

  const save = () => {
    const paise = Math.round(Number(rupees) * 100);
    if (!Number.isInteger(paise)) return;
    setSaving(true);
    setSaveError("");
    setSaveSuccess(false);
    updateCompanySettings(paise)
      .then((updated) => {
        onSettingsSaved(updated);
        setSaveSuccess(true);
        setTimeout(() => setSaveSuccess(false), 3000);
      })
      .catch((error: Error) => setSaveError(error.message))
      .finally(() => setSaving(false));
  };

  return (
    <section className="manager-workspace-container">
      <div className="eyebrow">Manager Workspace</div>
      <h1>Company Overview & Queue</h1>

      {overview && (
        <div className="overview-grid">
          <div className="stat-card">
            <strong>Pending Approval</strong>
            <span>{overview.pending_approval}</span>
          </div>
          <div className="stat-card">
            <strong>Auto Processed</strong>
            <span>{overview.auto_processed}</span>
          </div>
          <div className="stat-card">
            <strong>Manager Approved</strong>
            <span>{overview.approved}</span>
          </div>
          <div className="stat-card">
            <strong>Rejected / Denied</strong>
            <span>{overview.rejected + overview.denied}</span>
          </div>
          <div className="stat-card">
            <strong>Total Dispatched</strong>
            <span style={{ fontSize: "1.45rem" }}>{formatMoney(overview.total_refunded_paise)}</span>
          </div>
        </div>
      )}

      {/* Threshold Setting */}
      <div className="active-order-card" style={{ marginBottom: "28px" }}>
        <div className="eyebrow">Policy Configuration</div>
        <h3 style={{ margin: "4px 0 8px" }}>Autonomous Approval Threshold</h3>
        <p className="lede" style={{ marginBottom: "14px" }}>
          Claims at or below this value with clear damage/defect reasons will automatically approve without manager intervention.
        </p>
        <div style={{ display: "flex", gap: "10px", alignItems: "center" }}>
          <div style={{ position: "relative", width: "180px" }}>
            <span style={{ position: "absolute", left: "10px", top: "10px", color: "var(--muted)" }}>₹</span>
            <input
              type="number"
              value={rupees}
              onChange={(e) => setRupees(e.target.value)}
              style={{ width: "100%", padding: "9px 12px 9px 24px", border: "1px solid var(--border)", borderRadius: "var(--radius-sm)", background: "var(--surface)", color: "var(--text)" }}
            />
          </div>
          <button className="btn-claude-send" onClick={save} disabled={saving}>
            {saving ? "Saving..." : "Update Threshold"}
          </button>
          {saveSuccess && <span style={{ color: "var(--success)", fontSize: "0.85rem", fontWeight: "600" }}>✓ Updated successfully</span>}
          {saveError && <span style={{ color: "var(--danger)", fontSize: "0.85rem" }}>{saveError}</span>}
        </div>
      </div>

      {/* Review Queue */}
      <h2>Action Required: Review Queue ({items.length})</h2>
      <div className="table-wrapper" style={{ marginBottom: "32px" }}>
        <table>
          <thead>
            <tr>
              <th>Case ID</th>
              <th>Customer</th>
              <th>Reason</th>
              <th>Amount</th>
              <th>Failed Rules</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody>
            {items.length === 0 ? (
              <tr>
                <td colSpan={6} style={{ textAlign: "center", color: "var(--muted)", padding: "24px" }}>
                  Queue is clear. No cases currently awaiting manager decision.
                </td>
              </tr>
            ) : (
              items.map((item) => (
                <tr key={item.case_id}>
                  <td><code>{item.case_id.slice(0, 8)}</code></td>
                  <td>{item.customer_id}</td>
                  <td>{item.reason_category ?? "General"}</td>
                  <td>{formatMoney(item.amount_paise)}</td>
                  <td>
                    {item.failed_rules.length > 0 ? (
                      item.failed_rules.map((r) => (
                        <span key={r} className="rule-check-pill fail" style={{ marginRight: "4px" }}>{r}</span>
                      ))
                    ) : "—"}
                  </td>
                  <td>
                    <button className="btn-sidebar-sm" onClick={() => onOpen(item.case_id)}>
                      Review Case →
                    </button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* All Company Cases with Search & Filter */}
      <h2>All Inquiries & Claims</h2>
      <div className="manager-toolbar">
        <input
          type="text"
          className="search-input"
          placeholder="Search customer email, order ID, or reason..."
          value={searchQuery}
          onChange={(e) => onSearchChange(e.target.value)}
        />
        <select
          className="status-select"
          value={statusFilter}
          onChange={(e) => onStatusFilterChange(e.target.value)}
        >
          <option value="">All Statuses</option>
          <option value="pending_approval">Pending Approval</option>
          <option value="paid">Paid</option>
          <option value="open">Open</option>
          <option value="rejected">Rejected</option>
          <option value="denied">Denied</option>
          <option value="answered">Answered</option>
        </select>
      </div>

      <div className="table-wrapper">
        <table>
          <thead>
            <tr>
              <th>Case ID</th>
              <th>Customer</th>
              <th>Order ID</th>
              <th>Amount</th>
              <th>Status</th>
              <th>Decision</th>
              <th>Created</th>
              <th>Inspect</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={8} style={{ textAlign: "center", padding: "20px" }}>Loading records...</td></tr>
            ) : cases.length === 0 ? (
              <tr><td colSpan={8} style={{ textAlign: "center", color: "var(--muted)", padding: "20px" }}>No matching records found.</td></tr>
            ) : (
              cases.map((c) => (
                <tr key={c.case_id}>
                  <td><code>{c.case_id.slice(0, 8)}</code></td>
                  <td>{c.customer}</td>
                  <td>{c.order_id ?? "—"}</td>
                  <td>{formatMoney(c.amount_paise)}</td>
                  <td><StatusBadge status={c.status} /></td>
                  <td>{c.decision_type ?? "—"}</td>
                  <td>{formatRelativeTime(c.created_at)}</td>
                  <td>
                    <button className="btn-sidebar-sm" onClick={() => onOpen(c.case_id)}>
                      Inspect →
                    </button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

/* =========================================================
   DETAIL INSPECTION COMPONENT
   ========================================================= */
function Detail({
  caseData,
  onBack,
  onDecision,
}: {
  caseData: Case;
  onBack: () => void;
  onDecision: (action: "approve" | "deny") => void;
}) {
  return (
    <section className="detail-page-container">
      <button className="back-button" onClick={onBack}>
        ← Back to Overview
      </button>
      <div className="eyebrow" style={{ marginTop: "12px" }}>Case Audit Trail</div>
      <h1>Case #{caseData.id.slice(0, 13)}</h1>
      <p className="lede">Complete policy evaluation rules, state machine transitions, and reviewer decision.</p>

      <div style={{ display: "grid", gridTemplateColumns: "1.2fr 1fr", gap: "24px" }}>
        <div>
          <h2>Conversation History</h2>
          <div className="claude-messages-container" style={{ background: "var(--surface)", border: "1px solid var(--border)", borderRadius: "var(--radius-md)", maxHeight: "400px" }}>
            {caseData.messages.map((m, idx) => (
              <div key={idx} className={`chat-message-row ${m.role}`}>
                <div className="chat-message-avatar">{m.role === "customer" ? "👤" : "✨"}</div>
                <div className="chat-message-body-wrapper">
                  <span className="chat-sender-name">{m.role === "customer" ? "Customer" : "Assistant"}</span>
                  <div className="chat-message-card">{m.content}</div>
                </div>
              </div>
            ))}
          </div>

          <h2 style={{ marginTop: "24px" }}>Execution Timeline</h2>
          <Timeline steps={caseData.timeline} />
        </div>

        <div>
          <ClaudePipelinePanel
            caseData={caseData}
            onDecision={onDecision}
          />
        </div>
      </div>
    </section>
  );
}

/* =========================================================
   SHARED COMPONENTS: STATUS BADGE, TIMELINE, APPROVAL
   ========================================================= */
function StatusBadge({ status }: { status: string }) {
  return <span className={`status ${status}`}>{status.replaceAll("_", " ")}</span>;
}

function Timeline({ steps }: { steps: TimelineStep[] }) {
  return (
    <div className="timeline">
      {steps.map((step, index) => (
        <div className="timeline-step" key={`${step.node}-${index}`}>
          <div className={`timeline-dot ${step.status}`} />
          <div>
            <strong>{nodeLabels[step.node] ?? step.node}</strong>
            <span>{step.status.replace("_", " ")}</span>
          </div>
        </div>
      ))}
    </div>
  );
}

function ApprovalPanel({ onDecision }: { onDecision: (action: "approve" | "deny") => void }) {
  const [confirm, setConfirm] = useState<"approve" | "deny" | null>(null);

  return (
    <div className="approval-panel">
      <div className="eyebrow">Requires Human Authorization</div>
      <h3>Review Decision</h3>
      <p style={{ margin: "4px 0 10px", fontSize: "0.85rem", color: "var(--muted)" }}>
        Automated policy check escalated this case. Confirm your decision to resume the payout workflow.
      </p>
      {confirm ? (
        <div style={{ display: "flex", gap: "10px", alignItems: "center" }}>
          <span style={{ fontSize: "0.85rem", fontWeight: "600" }}>Confirm {confirm}?</span>
          <button
            className={confirm === "approve" ? "btn-approve" : "btn-deny"}
            onClick={() => onDecision(confirm)}
          >
            Yes, execute {confirm}
          </button>
          <button className="btn-sidebar-sm" onClick={() => setConfirm(null)}>Cancel</button>
        </div>
      ) : (
        <div className="approval-buttons">
          <button className="btn-approve" onClick={() => setConfirm("approve")}>
            Approve Payout
          </button>
          <button className="btn-deny" onClick={() => setConfirm("deny")}>
            Deny Refund
          </button>
        </div>
      )}
    </div>
  );
}

export default App;
