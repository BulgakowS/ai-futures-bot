import type { Metadata } from "next";
import "./styles.css";

export const metadata: Metadata = {
  title: "AI Futures · Paper Console",
  description: "Local paper trading console with MEXC public market data",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="uk"><body>{children}</body></html>;
}
