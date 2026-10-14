import type { Metadata } from "next";

export const metadata: Metadata = { title: "İnceleme", description: "Film sürümü incelemesi", robots: { index: false, follow: false } };

export default function ReviewLayout({ children }: { children: React.ReactNode }) {
  return children;
}
