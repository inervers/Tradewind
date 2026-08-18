// SVG 线条图标（stroke 风格，统一 1.6px）
import type { ReactNode, SVGProps } from "react";

type P = SVGProps<SVGSVGElement> & { size?: number };

function base({ size = 18, ...rest }: P, children: ReactNode) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...rest}
    >
      {children}
    </svg>
  );
}

export const MailIcon = (p: P) => base(p, <><rect x="3" y="5" width="18" height="14" rx="2" /><path d="m3 7 9 6 9-6" /></>);

export const BatchIcon = (p: P) => base(p, <><path d="M4 7h13v13H4z" /><path d="M7 4h13v13" /><path d="M8.5 11h4M8.5 14.5h7" /></>);

export const UsersIcon = (p: P) => base(p, <><circle cx="9" cy="8" r="3.2" /><path d="M3.5 19c.6-3 2.8-4.6 5.5-4.6s4.9 1.6 5.5 4.6" /><circle cx="17" cy="9.5" r="2.4" /><path d="M16.4 14.6c2.4.2 4 1.7 4.5 4" /></>);

export const BoxIcon = (p: P) => base(p, <><path d="M12 3 4 7v10l8 4 8-4V7z" /><path d="M4 7l8 4 8-4" /><path d="M12 11v10" /></>);

export const ClockIcon = (p: P) => base(p, <><circle cx="12" cy="12" r="8.5" /><path d="M12 7.5V12l3 2.2" /></>);

export const GearIcon = (p: P) => base(p, <><circle cx="12" cy="12" r="3" /><path d="M12 2.8v2.4M12 18.8v2.4M2.8 12h2.4M18.8 12h2.4M5.5 5.5l1.7 1.7M16.8 16.8l1.7 1.7M18.5 5.5l-1.7 1.7M7.2 16.8l-1.7 1.7" /></>);

export const PenIcon = (p: P) => base(p, <><path d="M4 20h4L19.5 8.5a2.1 2.1 0 0 0-3-3L5 17z" /><path d="m13.5 6.5 3 3" /></>);

export const TrashIcon = (p: P) => base(p, <><path d="M4 7h16" /><path d="M9 7V4.5h6V7" /><path d="M6.5 7l1 13h9l1-13" /><path d="M10 11v5M14 11v5" /></>);

export const CopyIcon = (p: P) => base(p, <><rect x="9" y="9" width="11" height="11" rx="2" /><path d="M5 15V5.5A1.5 1.5 0 0 1 6.5 4H15" /></>);

export const DownloadIcon = (p: P) => base(p, <><path d="M12 4v11" /><path d="m7.5 11 4.5 4.5L16.5 11" /><path d="M4.5 19.5h15" /></>);

export const PlusIcon = (p: P) => base(p, <><path d="M12 5v14M5 12h14" /></>);

export const SearchIcon = (p: P) => base(p, <><circle cx="11" cy="11" r="6.5" /><path d="m20 20-3.8-3.8" /></>);

export const SparkIcon = (p: P) => base(p, <><path d="M12 3.5 13.8 9l5.7 1.5-5.7 1.5L12 17.5l-1.8-5.5L4.5 10.5 10.2 9z" /><path d="M18.5 15.5l.8 2.4 2.4.8-2.4.8-.8 2.4-.8-2.4-2.4-.8 2.4-.8z" /></>);

export const CheckIcon = (p: P) => base(p, <><path d="m5 12.5 4.5 4.5L19 7" /></>);

export const XIcon = (p: P) => base(p, <><path d="m6 6 12 12M18 6 6 18" /></>);

export const UploadIcon = (p: P) => base(p, <><path d="M12 15V4" /><path d="m7.5 8.5 4.5-4.5L16.5 8.5" /><path d="M4.5 19.5h15" /></>);

export const RadarIcon = (p: P) => base(p, <><circle cx="12" cy="12" r="8.5" /><circle cx="12" cy="12" r="3" /><path d="M12 3.5v4M12 16.5v4M3.5 12h4M16.5 12h4" /><path d="m5.5 5.5 2.8 2.8M15.7 15.7l2.8 2.8M18.5 5.5l-2.8 2.8M8.3 15.7l-2.8 2.8" /></>);

export const PhotoIcon = (p: P) => base(p, <><rect x="3" y="4.5" width="18" height="15" rx="2" /><circle cx="8.5" cy="9" r="2" /><path d="m5 17 4.2-4.2 3.2 3 2.5-2.5L19 17" /></>);

export const SailboatIcon = (p: P) => base(p, <>
  <path d="M12 3.5v10.5" />
  <path d="M12 5 17.5 14H12z" />
  <path d="M12 5 6.8 14H12z" />
  <path d="M3.5 14h17l-1.8 3.2H5.3z" />
  <path d="M4.5 18.5q2-1.4 4 0t4 0 4 0 4 0" />
</>);

export const MailOpenIcon = (p: P) => base(p, <><path d="m3 8 9-5 9 5v10a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 18z" /><path d="m3 8.5 9 6 9-6" /></>);

export const InboxIcon = (p: P) => base(p, <><path d="M4 13.5 6.5 5h11L20 13.5V19a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 4 19z" /><path d="M4 13.5h5l1.5 2.5h3L15 13.5h5" /></>);

export const KeyIcon = (p: P) => base(p, <>
  <circle cx="8.5" cy="15" r="4.2" />
  <path d="M11.6 11.9 20 3.5" />
  <path d="M16.5 6.5l2.6 2.6" />
  <path d="M13.8 9.2l1.8 1.8" />
</>);
