type State = 'done' | 'partial' | 'planned'

const STAGES: { name: string; state: State; label: string; body: string }[] = [
  {
    name: 'Этап 1 · Очередь и дисциплина',
    state: 'done',
    label: 'работает',
    body: 'Ручной ввод, карточки, захват первым кликом, SLA-таймер, каденция касаний, передача старшему, WON/LOST, рейтинг, красный список, бэкапы. Без квиза допуска и каталога каналов MORIER.',
  },
  {
    name: 'Этап 2 · Охотник',
    state: 'done',
    label: 'работает',
    body: 'Сканер MTProto по каналам-донорам, детектор рекламы с чтением картинок, обогащение рекламодателя, скоринг v1, пул ключей Trustat, поиск по словам спроса на рекламу.',
  },
  {
    name: 'Этап 3 · Самообучение',
    state: 'partial',
    label: 'частично',
    body: 'Есть: проверка черновика учится на сообщениях, после которых клиент ответил, автопоиск новых доноров, недельный отчёт владельцу. Нет: скоринг v2 на своих данных и суфлёр по возражениям.',
  },
  {
    name: 'Этап 4 · Расширение',
    state: 'planned',
    label: 'впереди',
    body: 'Платный Trustat и база Telegram Ads, TGStat, переезд на PostgreSQL, веб-дашборд вместо этой страницы.',
  },
]

const DOT: Record<State, string> = {
  done: 'bg-primary',
  partial: 'border border-primary bg-transparent',
  planned: 'border border-muted-foreground bg-transparent',
}

const LABEL: Record<State, string> = {
  done: 'text-primary',
  partial: 'text-primary',
  planned: 'text-muted-foreground',
}

export function Roadmap() {
  return (
    <ul className="flex flex-col">
      {STAGES.map((stage) => (
        <li key={stage.name} className="flex gap-4 border-t border-border py-5 first:border-t-0 first:pt-0">
          <span aria-hidden="true" className={`mt-2 size-2 shrink-0 rounded-full ${DOT[stage.state]}`} />
          <div className="flex flex-col gap-1.5">
            <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
              <h3 className="text-base font-medium text-foreground text-balance">{stage.name}</h3>
              <span className={`font-mono text-xs uppercase tracking-wider ${LABEL[stage.state]}`}>
                {stage.label}
              </span>
            </div>
            <p className="text-sm leading-relaxed text-muted-foreground text-pretty">{stage.body}</p>
          </div>
        </li>
      ))}
    </ul>
  )
}
