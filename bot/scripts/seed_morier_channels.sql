-- Сид лидеров вертикалей для morier_channels (нужен гейту G2 «крупнее нашего лидера x2»).
-- Юзернеймы найдены поиском и проверены через t.me/s/<username> 2026-09-23:
-- название и тематика совпадают с постом-каталогом t.me/Morier_ads.
-- Применение: sqlite3 data/leadhunter.db < scripts/seed_morier_channels.sql
-- (INSERT OR IGNORE — повторный запуск безопасен; created_at можно поправить при желании.)

INSERT OR IGNORE INTO morier_channels (username, title, vertical, subscribers, reach_24h, flags, created_at) VALUES
  ('@mangodota',          'НОВОСТИ DOTA 2',    'dota',       100000, 0, '', '2026-09-23T00:00:00'),
  ('@clash_royale_newsw', 'Clash Royale News', 'clash',      189000, 0, '', '2026-09-23T00:00:00'),
  ('@steambyfree',        'Халявный Steam',    'steam',      123000, 0, '', '2026-09-23T00:00:00'),
  ('@twitchlive',         'ТВИЧ LIVE',         'streaming',  136000, 0, '', '2026-09-23T00:00:00'),
  ('@Haisenberg28',       'HAISENBERG',        'crypto',     731000, 0, '', '2026-09-23T00:00:00'),
  ('@rhymestg',           'Рифмы и Панчи',     'news',      1390000, 0, '', '2026-09-23T00:00:00'),
  ('@Novosty_Glavnyei',   'Мир трендов',       'trends',    3370000, 0, '', '2026-09-23T00:00:00');

-- НЕ верифицировано через превью (t.me/s отдаёт только контакт-страницу) — проверить руками перед раскомментированием:
-- INSERT OR IGNORE INTO morier_channels (username, title, vertical, subscribers, reach_24h, flags, created_at) VALUES
--   ('@games_news_tech', 'Игры и Патчи',  'gaming_news', 349000, 0, '', '2026-09-23T00:00:00'),
--   ('@fintypunch',      'Финты и Панчи', 'auto_sport',  145000, 0, '', '2026-09-23T00:00:00');

-- Юзернейм не найден поиском — добавить вручную из админки:
--   FORGE CS2 (скины, инвестиции), cs2, 58500   -- t.me/forgecs2 занят турнирным каналом, это другой
--   Brawl Stars, brawl, 702000                  -- крупнейший BS-канал сети, имя без юзернейма в посте
