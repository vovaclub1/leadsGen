const BLOCKS = [
  {
    caption: 'Запуск на сервере',
    lines: ['cd bot', 'cp .env.example .env    # BOT_TOKEN и OWNER_ID', 'docker compose up -d --build'],
  },
  {
    caption: 'Тесты — без сети и без Telegram',
    lines: ['BOT_TOKEN=1:x OWNER_ID=1 AI_API_KEY=sk-test \\', '  DATA_DIR=/tmp/lh python -m tests.harness'],
  },
]

export function Quickstart() {
  return (
    <div className="grid gap-4 sm:grid-cols-2">
      {BLOCKS.map((block) => (
        <div key={block.caption} className="flex flex-col gap-2">
          <p className="font-mono text-xs uppercase tracking-wider text-muted-foreground">{block.caption}</p>
          <pre className="overflow-x-auto rounded-lg border border-border bg-card p-4 font-mono text-xs leading-relaxed text-foreground">
            <code>{block.lines.join('\n')}</code>
          </pre>
        </div>
      ))}
    </div>
  )
}
