CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  username TEXT,
  full_name TEXT,
  role TEXT NOT NULL CHECK (role IN ('owner', 'senior', 'sdr', 'buyer')),
  status TEXT NOT NULL DEFAULT 'active',
  added_by INTEGER,
  created_at TEXT NOT NULL,
  last_seen TEXT
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS leads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_key TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'channel',
  username TEXT,
  title TEXT,
  url TEXT,
  about TEXT,
  subscribers INTEGER,
  avg_views INTEGER,
  recent_posts TEXT,
  source TEXT NOT NULL,
  donor TEXT,
  keyword TEXT,
  ad_text TEXT,
  ad_count INTEGER NOT NULL DEFAULT 0,
  niche TEXT,
  vertical TEXT,
  ai_json TEXT,
  score INTEGER NOT NULL DEFAULT 0,
  category TEXT NOT NULL DEFAULT 'cold',
  risk_topic TEXT NOT NULL DEFAULT 'none',
  gate_note TEXT,
  contact_username TEXT,
  contact_user_id INTEGER,
  contact_state TEXT NOT NULL DEFAULT 'ok',
  status TEXT NOT NULL DEFAULT 'NEW',
  assigned_to INTEGER,
  claimed_at TEXT,
  contact_deadline TEXT,
  sla_warned INTEGER NOT NULL DEFAULT 0,
  contacted_at TEXT,
  first_message TEXT,
  replied_at TEXT,
  touch_count INTEGER NOT NULL DEFAULT 0,
  next_touch_at TEXT,
  handoff_at TEXT,
  handoff_note TEXT,
  accepted_by INTEGER,
  accepted_at TEXT,
  closed_at TEXT,
  lost_reason TEXT,
  won_amount INTEGER,
  won_margin INTEGER,
  note TEXT,
  group_msg_id INTEGER,
  aging_flags TEXT NOT NULL DEFAULT '',
  trustat_json TEXT,
  created_by INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leads_key ON leads (entity_key);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads (status);
CREATE INDEX IF NOT EXISTS idx_leads_assigned ON leads (assigned_to);

CREATE TABLE IF NOT EXISTS lead_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lead_id INTEGER NOT NULL,
  user_id INTEGER,
  type TEXT NOT NULL,
  payload TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_lead ON lead_events (lead_id);

CREATE TABLE IF NOT EXISTS points (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  delta INTEGER NOT NULL,
  reason TEXT,
  lead_id INTEGER,
  created_by INTEGER,
  season TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_points_user ON points (user_id, season);

CREATE TABLE IF NOT EXISTS dnc_contacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  username TEXT,
  level TEXT NOT NULL CHECK (level IN ('red', 'orange', 'yellow')),
  reason TEXT,
  added_by INTEGER,
  until TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS blacklist (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_key TEXT UNIQUE NOT NULL,
  reason TEXT,
  added_by INTEGER,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider TEXT NOT NULL DEFAULT 'trustat',
  key_enc TEXT NOT NULL,
  key_tail TEXT,
  label TEXT,
  scopes TEXT NOT NULL DEFAULT '',
  limit_day INTEGER NOT NULL DEFAULT 0,
  limit_month INTEGER NOT NULL DEFAULT 0,
  used_day INTEGER NOT NULL DEFAULT 0,
  used_month INTEGER NOT NULL DEFAULT 0,
  day_key TEXT,
  month_key TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  last_error TEXT,
  plan TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS keywords (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  word TEXT NOT NULL,
  api_key_id INTEGER,
  last_run TEXT,
  found INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS donors (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  title TEXT,
  vertical TEXT,
  joined INTEGER NOT NULL DEFAULT 0,
  ads_found INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1,
  added_by INTEGER,
  created_at TEXT NOT NULL
);

-- Кандидаты в доноры: каналы, которые Telegram считает похожими на наших действующих доноров.
CREATE TABLE IF NOT EXISTS donor_hints (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  title TEXT,
  subscribers INTEGER,
  votes INTEGER NOT NULL DEFAULT 1,
  sources TEXT,
  status TEXT NOT NULL DEFAULT 'new',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ad_posts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  donor TEXT NOT NULL,
  msg_id INTEGER,
  advertiser_key TEXT,
  text TEXT,
  confidence REAL,
  lead_id INTEGER,
  views INTEGER,
  created_at TEXT NOT NULL,
  image_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_ads_adv ON ad_posts (advertiser_key);

CREATE TABLE IF NOT EXISTS ai_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  month TEXT NOT NULL,
  tokens_in INTEGER NOT NULL DEFAULT 0,
  tokens_out INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0,
  purpose TEXT,
  created_at TEXT NOT NULL,
  ok INTEGER NOT NULL DEFAULT 1,
  error TEXT
);

-- Каналы MORIER (ТЗ 8.1): каталог, вилка цен, матрица размещаемости.
-- buy_price видит только владелец; flags — тематики, которые канал ПРИНИМАЕТ (betting, casino, crypto, adult, vpn).
CREATE TABLE IF NOT EXISTS morier_channels (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  title TEXT,
  vertical TEXT NOT NULL,
  subscribers INTEGER,
  reach_24h INTEGER,
  price_from INTEGER,
  price_to INTEGER,
  buy_price INTEGER,
  category TEXT NOT NULL DEFAULT 'B' CHECK (category IN ('A', 'B', 'C')),
  flags TEXT NOT NULL DEFAULT '',
  admin_contact TEXT,
  stat_updated_at TEXT,
  created_at TEXT NOT NULL
);
