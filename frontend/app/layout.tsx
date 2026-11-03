import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Карточка ВУЗа — рабочее пространство",
  description: "Карточки образовательных организаций и помощник по работе с данными",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="ru">
      <body>{children}</body>
    </html>
  );
}
