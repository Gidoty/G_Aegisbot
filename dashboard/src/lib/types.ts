export interface Control {
  paused: boolean;
  flatten: boolean;
  reason: string | null;
  scope: string | null;
}

export interface Me {
  user_id: string;
  is_admin: boolean;
  trading_mode: "live" | "testnet";
  leverage: number;
  max_wallet_cap_usdt: number;
  virtual_balance: number;
  active_coins: string[];
  keys_stored: boolean;
  subscription: { status: string; expires_at: string | null; active: boolean };
  today: {
    date: string | null;
    start_equity: number | null;
    pnl: number | null;
    loss_pct: number | null;
    breaker_tripped: boolean;
  };
  control: Control;
}

export interface Position {
  symbol: string;
  side: "long" | "short";
  entry_price: number;
  stop_price: number;
  best_price: number;
  leverage: number;
  mode: string;
  strategy?: string;
  opened_at: string;
}

export interface Trade {
  symbol: string;
  side: "long" | "short";
  amount: number;
  entry_price: number;
  exit_price: number | null;
  pnl: number | null;
  fees: number;
  reason: string;
  mode: string;
  strategy?: string;
  opened_at: string;
  closed_at: string;
}

export interface EquityPoint {
  at: string;
  equity: number;
}

export interface Runner {
  alive: boolean;
  seconds_since: number | null;
  info: string | null;
}

export interface Health {
  status: string;
  db: boolean;
  runner: Runner;
  billing_enabled: boolean;
}

export interface Billing {
  enabled: boolean;
  price_usd: number;
  days: number;
  payments: { order_id: string; status: string; price_usd: number; days: number; credited: boolean; created_at: string }[];
}

export interface AdminOverview {
  runner: Runner;
  kill_switch: { scope: string; flatten: boolean; reason: string | null; set_by: string | null }[];
  open_trades: number;
  users: Me[];
}

export interface AuditRow {
  at: string;
  actor: string;
  action: string;
  target: string | null;
  detail: string | null;
}

export interface PaymentRow {
  order_id: string;
  user_id: string;
  status: string;
  price_usd: number;
  days: number;
  credited: boolean;
  payment_id: string | null;
  created_at: string;
}

export const DEFAULT_PAIRS = [
  "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT", "XRP/USDT:USDT",
  "LINK/USDT:USDT", "ADA/USDT:USDT", "AVAX/USDT:USDT", "NEAR/USDT:USDT", "SUI/USDT:USDT",
  "DOGE/USDT:USDT", "FET/USDT:USDT", "1000PEPE/USDT:USDT", "RENDER/USDT:USDT", "DOT/USDT:USDT",
];
