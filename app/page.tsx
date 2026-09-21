import { Discipline } from '@/components/site/discipline'
import { LeadCard } from '@/components/site/lead-card'
import { Pipeline } from '@/components/site/pipeline'
import { Quickstart } from '@/components/site/quickstart'
import { Roadmap } from '@/components/site/roadmap'

const FACTS = [
  'Python 3.12 · aiogram 3 · Telethon · SQLite',
  'Сервер 1 vCPU / 2 GB',
  'Владение ≈ 700–1 400 ₽ в месяц',
]

function SectionTitle({ children }: { children: React.ReactNode }) {
  return <h2 className="font-mono text-xs uppercase tracking-widest text-muted-foreground">{children}</h2>
}

export default function Page() {
  return (
    <div className="min-h-screen bg-background">
      <div className="mx-auto w-full max-w-3xl px-5 py-16 sm:px-8 sm:py-24">
        <div className="flex flex-col gap-16 sm:gap-20">
          <header className="flex flex-col gap-5">
            <p className="font-mono text-xs uppercase tracking-widest text-primary">
              MORIER · посевы в Telegram
            </p>
            <h1 className="text-4xl font-medium tracking-tight text-foreground sm:text-5xl">LeadHunter</h1>
            <p className="max-w-xl text-base leading-relaxed text-muted-foreground text-pretty sm:text-lg">
              Бот читает каналы-доноры, ловит каждый рекламный пост, достаёт рекламодателя, изучает его ИИ
              и через минуту выкладывает команде карточку с готовым фактом для первого сообщения.
            </p>
            <p className="max-w-xl text-base leading-relaxed text-foreground text-pretty">
              Клиенту пишет человек. Бот не отправляет за него ни одного сообщения — иначе агентство теряет
              и аккаунты, и то единственное, чем отличается от рассыльщиков: персонализацию.
            </p>
            <ul className="flex flex-wrap gap-2">
              {FACTS.map((fact) => (
                <li
                  key={fact}
                  className="rounded-md border border-border px-3 py-1.5 font-mono text-xs text-muted-foreground"
                >
                  {fact}
                </li>
              ))}
            </ul>
          </header>

          <section className="flex flex-col gap-4">
            <SectionTitle>Путь лида</SectionTitle>
            <Pipeline />
          </section>

          <section className="flex flex-col gap-4">
            <SectionTitle>Что получает команда</SectionTitle>
            <LeadCard />
          </section>

          <section className="flex flex-col gap-4">
            <SectionTitle>На чём держится дисциплина</SectionTitle>
            <Discipline />
          </section>

          <section className="flex flex-col gap-4">
            <SectionTitle>Состояние по этапам ТЗ</SectionTitle>
            <Roadmap />
          </section>

          <section className="flex flex-col gap-4">
            <SectionTitle>Запуск</SectionTitle>
            <Quickstart />
          </section>

          <footer className="flex flex-col gap-2 border-t border-border pt-8">
            <p className="font-mono text-xs leading-relaxed text-muted-foreground">
              bot/ — код и инструкция · docs/TZ_MORIER_LeadHunter_Bot.md — техническое задание v1.2
            </p>
            <p className="text-sm leading-relaxed text-muted-foreground text-pretty">
              Доступ к боту — только по Telegram ID из белого списка владельца. Всем остальным бот не отвечает.
            </p>
          </footer>
        </div>
      </div>
    </div>
  )
}
