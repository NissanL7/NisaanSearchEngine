import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'NisaanSearchEngine',
  description: 'Search the web with NisaanSearchEngine.',
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
