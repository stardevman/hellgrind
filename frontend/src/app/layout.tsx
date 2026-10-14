import type { Metadata, Viewport } from "next";
import "./globals.css";

const ICON = "data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 32 32%27%3E%3Crect width=%2732%27 height=%2732%27 rx=%277%27 fill=%27%23e8783a%27/%3E%3Cpath d=%27M16 5c1 5 7 7 7 13.5a7 7 0 0 1-14 0c0-3 2-4.2 3-7 1 2 2 3 3 3 0-3 0-6 1-9.5z%27 fill=%27%2324100a%27/%3E%3C/svg%3E";

export const metadata: Metadata = {
  title: "Hell Grind Stüdyo",
  description: "Otonom animasyon hattının yapım stüdyosu",
  icons: { icon: ICON },
};

export const viewport: Viewport = { width: "device-width", initialScale: 1, colorScheme: "dark", themeColor: "#19191c" };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="tr">
      <head>
        {/* Inter (arayüz) ve JetBrains Mono (zaman kodu, çekim kodları); çevrimdışıyken sistem yazı tiplerine düşer. */}
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="" />
        {/* eslint-disable-next-line @next/next/no-page-custom-font */}
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet" />
      </head>
      <body>{children}</body>
    </html>
  );
}
