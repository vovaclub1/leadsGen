const FIELDS = [
  { label: 'Вертикаль', value: 'Steam, CS2' },
  { label: 'Источник', value: 'реклама в @forge_cs2 · 3 размещения за 30 дней' },
  { label: 'Почему клиент', value: 'магазин ключей, закупает рекламу в гейминге каждую неделю' },
  { label: 'Продукт', value: 'сайт и бот-магазин, оплата картой' },
  { label: 'Контакт', value: '@keydrop_manager — «по рекламе» в описании' },
  { label: 'Факт для первого сообщения', value: '«заметил ваше размещение у FORGE CS2 на этой неделе»' },
]

export function LeadCard() {
  return (
    <figure className="flex flex-col gap-3">
      <div className="overflow-hidden rounded-lg border border-border bg-card">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-border bg-primary/10 px-4 py-3">
          <span aria-hidden="true" className="size-2 rounded-full bg-primary" />
          <span className="font-mono text-sm font-medium tracking-wide text-primary">ГОРЯЧИЙ · 82</span>
          <span className="font-mono text-sm text-muted-foreground">лид #1042</span>
        </div>

        <div className="flex flex-col gap-4 px-4 py-4">
          <div className="flex flex-col gap-1">
            <h3 className="text-lg font-medium text-foreground">KeyDrop RU</h3>
            <p className="font-mono text-xs text-muted-foreground">
              @keydrop_ru · 14 200 подп. · ~9 800 просмотров на пост · активен
            </p>
          </div>

          <dl className="flex flex-col gap-2 text-sm">
            {FIELDS.map((field) => (
              <div key={field.label} className="flex flex-col gap-0.5 sm:flex-row sm:gap-2">
                <dt className="shrink-0 font-mono text-xs uppercase tracking-wider text-muted-foreground sm:w-44 sm:pt-0.5">
                  {field.label}
                </dt>
                <dd className="leading-relaxed text-foreground text-pretty">{field.value}</dd>
              </div>
            ))}
          </dl>

          <div className="flex flex-wrap gap-2 border-t border-border pt-4">
            <span className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground">
              Беру
            </span>
            <span className="rounded-md border border-border px-3 py-1.5 text-sm text-muted-foreground">
              Нецелевой
            </span>
            <span className="rounded-md border border-border px-3 py-1.5 text-sm text-muted-foreground">
              Дубликат
            </span>
          </div>
        </div>
      </div>

      <figcaption className="text-sm leading-relaxed text-muted-foreground text-pretty">
        Так карточка приходит в группу команды. Захват — атомарная операция: лид достаётся первому нажавшему,
        остальные видят «уже взял @ivan». С этой секунды у продавца 30 минут на первое сообщение клиенту.
      </figcaption>
    </figure>
  )
}
