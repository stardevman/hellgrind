/* Satır içi SVG simgeleri (dış kaynak yok). Hepsi currentColor ile boyanır. */
import type { ReactElement } from "react";

const S = (d: ReactElement) => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{d}</svg>
);

export const Icon = {
  logo: () => (
    <svg viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="7" fill="#4d8dff" />
      <path d="M16 5c1 5 7 7 7 13.5a7 7 0 0 1-14 0c0-3 2-4.2 3-7 1 2 2 3 3 3 0-3 0-6 1-9.5z" fill="#0d1526" /></svg>
  ),
  script: () => S(<><path d="M6 3h9l4 4v14H6z" /><path d="M15 3v4h4M9 11h6M9 14h6M9 17h4" /></>),
  style: () => S(<><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></>),
  assets: () => S(<><path d="M12 2l9 5v10l-9 5-9-5V7z" /><path d="M3 7l9 5 9-5M12 12v10" /></>),
  board: () => S(<><rect x="3" y="4" width="8" height="6" rx="1" /><rect x="13" y="4" width="8" height="6" rx="1" /><rect x="3" y="14" width="8" height="6" rx="1" /><rect x="13" y="14" width="8" height="6" rx="1" /></>),
  edit: () => S(<><rect x="2" y="4" width="20" height="16" rx="2" /><path d="M2 14h20M7 14v6M12 14v6M17 14v6M10 7.5l4 2-4 2z" /></>),
  compare: () => S(<><rect x="2" y="5" width="9" height="14" rx="1" /><rect x="13" y="5" width="9" height="14" rx="1" /><path d="M12 2v20" /></>),
  color: () => S(<><circle cx="8" cy="9" r="5" /><circle cx="16" cy="9" r="5" /><circle cx="12" cy="15" r="5" /></>),
  deliver: () => S(<><path d="M12 15V3M7 8l5-5 5 5" /><path d="M4 14v5a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-5" /></>),
  play: () => <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 4.5v15l12.5-7.5z" fill="currentColor" /></svg>,
  pause: () => <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6.5 4.5h4v15h-4zM13.5 4.5h4v15h-4z" fill="currentColor" /></svg>,
  stepBack: () => S(<><path d="M6 5v14" /><path d="M18 5l-9 7 9 7z" fill="currentColor" /></>),
  stepFwd: () => S(<><path d="M18 5v14" /><path d="M6 5l9 7-9 7z" fill="currentColor" /></>),
  toStart: () => S(<><path d="M5 5v14" /><path d="M19 5l-8 7 8 7z" fill="currentColor" /><path d="M12 5l-6 7 6 7" /></>),
  loop: () => S(<><path d="M17 2l4 4-4 4" /><path d="M3 11V9a3 3 0 0 1 3-3h15M7 22l-4-4 4-4" /><path d="M21 13v2a3 3 0 0 1-3 3H3" /></>),
  film: () => S(<><rect x="3" y="3" width="18" height="18" rx="2" /><path d="M7 3v18M17 3v18M3 8h4M3 16h4M17 8h4M17 16h4" /></>),
  file: () => S(<><path d="M6 3h9l4 4v14H6z" /><path d="M15 3v4h4" /></>),
  zip: () => S(<><path d="M6 3h12v18H6z" /><path d="M12 3v2M12 7v2M12 11v2M10 15h4v3h-4z" /></>),
  list: () => S(<><path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01" /></>),
  plus: () => S(<path d="M12 5v14M5 12h14" />),
  receipt: () => S(<><path d="M6 2h12v20l-3-2-3 2-3-2-3 2z" /><path d="M9 7h6M9 11h6M9 15h4" /></>),
  copy: () => S(<><rect x="9" y="9" width="12" height="12" rx="2" /><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" /></>),
  gear: () => S(<><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" /></>),
};
