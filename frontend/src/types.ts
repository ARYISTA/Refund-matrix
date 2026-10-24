export type CaseStatus =
  | "open"
  | "pending_approval"
  | "paid"
  | "denied"
  | "rejected"
  | "answered"
  | "handed_off";

export type NodeName =
  | "triage"
  | "resolution"
  | "refund_intake"
  | "load_facts"
  | "policy_check"
  | "human_review"
  | "payout"
  | "responder";

export type NodeStatus = "started" | "completed" | "failed" | "interrupted" | "resumed";

export interface PolicyRule {
  rule: string;
  passed: boolean;
  reason: string;
}

export interface PolicyResult {
  decision: "AUTO_APPROVE" | "REJECT" | "ESCALATE";
  rules: PolicyRule[];
  policy_version: string;
  amount_paise: number;
  currency: "INR";
}

export interface Message {
  role: "customer" | "assistant";
  content: string;
}

export interface TimelineStep {
  node: NodeName;
  status: NodeStatus;
  at: string;
}

export interface Payout {
  payout_id: string;
  utr: string;
  amount_paise: number;
  currency: "INR";
  idempotency_key: string;
  account_masked: string;
  created_at: string;
}

export interface Case {
  id: string;
  thread_id: string;
  status: CaseStatus;
  created_at: string;
  updated_at: string;
  messages: Message[];
  timeline: TimelineStep[];
  policy_result?: PolicyResult | null;
  amount_paise?: number | null;
  payout?: Payout | null;
  awaiting_approval: boolean;
}

export interface CustomerCase {
  id: string;
  thread_id: string;
  status: "processed" | "under_review" | "not_eligible" | "rejected" | "answered" | "handed_off";
  reply?: string | null;
  messages: Message[];
  created_at: string;
  updated_at: string;
  order_id?: string | null;
  amount_paise?: number | null;
  policy_result?: PolicyResult | null;
  timeline?: TimelineStep[];
  reason?: string | null;
}

export interface QueueItem {
  case_id: string;
  customer_id: string;
  status: CaseStatus;
  amount_paise?: number | null;
  reason_category?: string | null;
  failed_rules: string[];
  created_at: string;
}

export interface CompanyOverview {
  pending_approval: number;
  auto_processed: number;
  approved: number;
  denied: number;
  rejected: number;
  total_refunded_paise: number;
}

export interface CompanyCase {
  case_id: string;
  customer: string;
  order_id?: string | null;
  amount_paise?: number | null;
  reason?: string | null;
  status: CaseStatus;
  decision_type?: string | null;
  created_at: string;
}

export interface CompanySettings {
  threshold_paise: number;
  version: number;
  updated_by: string;
  updated_at: string;
}

export interface CustomerOrderItem {
  item_id: string;
  name: string;
  quantity: number;
  unit_price_paise: number;
}

export interface CustomerOrder {
  order_id: string;
  ordered_at: string;
  delivered_at: string | null;
  total_paise: number;
  payment_status: string;
  items: CustomerOrderItem[];
  can_refund: boolean;
}

export interface CustomerCaseSummary {
  case_id: string;
  order_id?: string | null;
  amount_paise?: number | null;
  status: "processed" | "under_review" | "not_eligible" | "rejected" | "answered" | "handed_off";
  reply?: string | null;
  created_at: string;
  updated_at: string;
}

