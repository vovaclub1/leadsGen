import { Analytics } from '@vercel/analytics/next'
import { Inter, JetBrains_Mono } from 'next/font/google'
import type { Metadata, Viewport } from 'next'
import './globals.css'

const _inter = Inter({ subsets: ['latin', 'cyrillic'] })
const _jetBrainsMono = JetBrains_Mono({ subsets: ['latin', 'cyrillic'] })

export const metadata: Metadata = {
  title: 'MORIER LeadHunter — бот поиска рекламодателей',
  description:
    'Telegram-бот агентства посевов MORIER: находит рекламодателей с бюджетом, изучает их ИИ, раздаёт лиды команде по правилу первого клика и держит SLA первого контакта.',
  generator: 'v0.app',
  icons: {
    icon: [
      {
        url: '/icon-light-32x32.png',
        media: '(prefers-color-scheme: light)',
      },
      {
        url: '/icon-dark-32x32.png',
        media: '(prefers-color-scheme: dark)',
      },
      {
        url: '/icon.svg',
        type: 'image/svg+xml',
      },
    ],
    apple: '/apple-icon.png',
  },
}

export const viewport: Viewport = {
  colorScheme: 'dark',
  themeColor: '#14161a',
}

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode
}>) {
  return (
    <html lang="ru" className="dark bg-background">
      <body className="antialiased font-sans">
        {children}
        {process.env.NODE_ENV === 'production' && <Analytics />}
      </body>
    </html>
  )
}
