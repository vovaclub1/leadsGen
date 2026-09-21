const STEPS = [
  { title: 'Сканер', note: 'MTProto читает каналы-доноры' },
  { title: 'Дедуп', note: 'стоп-листы, cooldown, красный список' },
  { title: 'Обогащение', note: 'подписчики, посты, контакт' },
  { title: 'ИИ-анализ', note: 'ниша, продукт, личный факт' },
  { title: 'Скоринг', note: 'горячий / тёплый / холодный' },
  { title: 'Очередь', note: 'карточка в группе команды' },
]

export function Pipeline() {
  return (
    <ol className="flex flex-wrap items-stretch gap-2">
      {STEPS.map((step, index) => (
        <li
          key={step.title}
          className="flex flex-1 basis-40 items-center gap-2 rounded-md border border-border bg-card p-3"
        >
          <div className="flex flex-col gap-1">
            <span className="font-mono text-sm text-foreground">{step.title}</span>
            <span className="text-xs leading-relaxed text-muted-foreground text-pretty">{step.note}</span>
          </div>
          {index < STEPS.length - 1 && (
            <span aria-hidden="true" className="ml-auto font-mono text-muted-foreground">
              →
            </span>
          )}
        </li>
      ))}
    </ol>
  )
}
