import type { Metadata, Viewport } from "next";
import "./globals.css";

const ICON = "data:image/svg+xml,%3Csvg xmlns=%27http://www.w3.org/2000/svg%27 viewBox=%270 0 32 32%27%3E%3Crect width=%2732%27 height=%2732%27 rx=%276%27 fill=%27%232A2D31%27/%3E%3Cpath d=%27M16 4c1 5 7 7 7 14a7 7 0 0 1-14 0c0-3 2-4 3-7 1 2 2 3 3 3 0-3 0-6 1-10z%27 fill=%27%23E4A23E%27/%3E%3C/svg%3E";

export const metadata: Metadata = {
  title: "Hell Grind — Kontrol Paneli",
  description: "Otonom animasyon hattının kontrol paneli",
  icons: { icon: ICON },
};

export const viewport: Viewport = { width: "device-width", initialScale: 1, colorScheme: "dark", themeColor: "#2A2D31" };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="tr">
      <head>
        {/* Archivo, genişlik (wdth) ekseniyle; çevrimdışıyken sistem yazı tipine düşer, işlev etkilenmez. */}
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="" />
        {/* eslint-disable-next-line @next/next/no-page-custom-font */}
        <link href="https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@62..125,100..900&display=swap" rel="stylesheet" />
      </head>
      <body>{children}</body>
    </html>
  );
}
