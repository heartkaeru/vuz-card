import type { SVGProps } from "react";

export type IconName =
  | "overview"
  | "university"
  | "calendar"
  | "file"
  | "search"
  | "chevron"
  | "plus"
  | "send"
  | "close"
  | "filter"
  | "download"
  | "trash"
  | "arrow"
  | "spark"
  | "check"
  | "pin"
  | "mail"
  | "phone"
  | "external"
  | "dots";

type IconProps = SVGProps<SVGSVGElement> & { name: IconName; size?: number };

export function Icon({ name, size = 20, ...props }: IconProps) {
  const common = {
    width: size,
    height: size,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.7,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": true,
    ...props,
  };

  switch (name) {
    case "overview":
      return <svg {...common}><rect x="3.5" y="3.5" width="7" height="7" rx="2" /><rect x="13.5" y="3.5" width="7" height="7" rx="2" /><rect x="3.5" y="13.5" width="7" height="7" rx="2" /><rect x="13.5" y="13.5" width="7" height="7" rx="2" /></svg>;
    case "university":
      return <svg {...common}><path d="m3 9 9-5 9 5" /><path d="M5 10v8m4-8v8m6-8v8m4-8v8M3 20h18M2.5 9.5h19" /></svg>;
    case "calendar":
      return <svg {...common}><rect x="3.5" y="5" width="17" height="15" rx="2.5" /><path d="M7.5 3.5v3M16.5 3.5v3M4 9.5h16" /><path d="M8 13h.01M12 13h.01M16 13h.01M8 16.5h.01M12 16.5h.01" strokeWidth="2.4" /></svg>;
    case "file":
      return <svg {...common}><path d="M6 3.75h8l4 4V20a.75.75 0 0 1-.75.75h-11.5A.75.75 0 0 1 5 20V4.5a.75.75 0 0 1 1-.75Z" /><path d="M14 4v4h4M8 13h8M8 16.5h8" /></svg>;
    case "search":
      return <svg {...common}><circle cx="10.8" cy="10.8" r="6.6" /><path d="m16 16 4.3 4.3" /></svg>;
    case "chevron":
      return <svg {...common}><path d="m7 9 5 5 5-5" /></svg>;
    case "plus":
      return <svg {...common}><path d="M12 5v14M5 12h14" /></svg>;
    case "send":
      return <svg {...common}><path d="m21 3-7.2 18-3.3-7.5L3 10.2 21 3Z" /><path d="M10.5 13.5 21 3" /></svg>;
    case "close":
      return <svg {...common}><path d="m6 6 12 12M18 6 6 18" /></svg>;
    case "filter":
      return <svg {...common}><path d="M4 6h16M7 12h10m-7 6h4" /></svg>;
    case "download":
      return <svg {...common}><path d="M12 3.5v11M7.5 10l4.5 4.5 4.5-4.5M4 17.5v2a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-2" /></svg>;
    case "trash":
      return <svg {...common}><path d="M4.5 7h15M9 7V4.5h6V7m-8 0 .8 13h8.4L17 7M10 10.5v6M14 10.5v6" /></svg>;
    case "arrow":
      return <svg {...common}><path d="M5 12h13M13 6l6 6-6 6" /></svg>;
    case "spark":
      return <svg {...common}><path d="m12 2.8 1.65 6.15L19.8 10.6l-6.15 1.65L12 18.4l-1.65-6.15L4.2 10.6l6.15-1.65L12 2.8Z" /><path d="m19 15 .8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8L19 15Z" /></svg>;
    case "check":
      return <svg {...common}><path d="m5.5 12.2 4 4.1L18.7 7" /></svg>;
    case "pin":
      return <svg {...common}><path d="M19 10c0 5-7 11-7 11s-7-6-7-11a7 7 0 1 1 14 0Z" /><circle cx="12" cy="10" r="2.2" /></svg>;
    case "mail":
      return <svg {...common}><rect x="3.5" y="5" width="17" height="14" rx="2" /><path d="m4.5 7 7.5 6 7.5-6" /></svg>;
    case "phone":
      return <svg {...common}><path d="M7.2 3.5h-2a1.7 1.7 0 0 0-1.7 1.8c.5 8.4 6.8 14.7 15.2 15.2a1.7 1.7 0 0 0 1.8-1.7v-2.1l-4.3-2-2 2a12.3 12.3 0 0 1-6.4-6.4l2-2-2-4.8Z" /></svg>;
    case "external":
      return <svg {...common}><path d="M13 5h6v6M19 5l-9 9" /><path d="M18 13v5a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5" /></svg>;
    case "dots":
      return <svg {...common} stroke="none" fill="currentColor"><circle cx="5" cy="12" r="1.7" /><circle cx="12" cy="12" r="1.7" /><circle cx="19" cy="12" r="1.7" /></svg>;
  }
}
