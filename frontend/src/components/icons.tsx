import type { SVGProps } from 'react';

/** Decorative by default: every icon is hidden from assistive tech; buttons carry the label. */
function Icon({ children, ...props }: SVGProps<SVGSVGElement>) {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      {...props}
    >
      {children}
    </svg>
  );
}

type P = SVGProps<SVGSVGElement>;

export const ArrowUpIcon = (p: P) => (
  <Icon {...p}>
    <path d="M8 13V3M3.5 7.5 8 3l4.5 4.5" />
  </Icon>
);
export const StopIcon = (p: P) => (
  <Icon {...p}>
    <rect x="4" y="4" width="8" height="8" rx="1.5" fill="currentColor" stroke="none" />
  </Icon>
);
export const PlusIcon = (p: P) => (
  <Icon {...p}>
    <path d="M8 3v10M3 8h10" />
  </Icon>
);
export const SunIcon = (p: P) => (
  <Icon {...p}>
    <circle cx="8" cy="8" r="2.75" />
    <path d="M8 1.5v1.25M8 13.25v1.25M1.5 8h1.25M13.25 8h1.25M3.4 3.4l.9.9M11.7 11.7l.9.9M3.4 12.6l.9-.9M11.7 4.3l.9-.9" />
  </Icon>
);
export const MoonIcon = (p: P) => (
  <Icon {...p}>
    <path d="M13.5 9.4A5.75 5.75 0 0 1 6.6 2.5a5.75 5.75 0 1 0 6.9 6.9Z" />
  </Icon>
);
export const CopyIcon = (p: P) => (
  <Icon {...p}>
    <rect x="5.5" y="5.5" width="8" height="8" rx="1.75" />
    <path d="M10.5 5.5V4.25A1.75 1.75 0 0 0 8.75 2.5h-4.5A1.75 1.75 0 0 0 2.5 4.25v4.5c0 .97.78 1.75 1.75 1.75H5.5" />
  </Icon>
);
export const CheckIcon = (p: P) => (
  <Icon {...p}>
    <path d="m3.5 8.5 3 3 6-7" />
  </Icon>
);
export const ChevronIcon = (p: P) => (
  <Icon {...p}>
    <path d="m6 4 4 4-4 4" />
  </Icon>
);
export const AlertIcon = (p: P) => (
  <Icon {...p}>
    <path d="M8 2.25 14.25 13H1.75L8 2.25Z" />
    <path d="M8 6.5v3M8 11.25v.01" />
  </Icon>
);
export const ShieldIcon = (p: P) => (
  <Icon {...p}>
    <path d="M8 1.75 13 3.5v4.1c0 3-2.05 5.3-5 6.65-2.95-1.35-5-3.65-5-6.65V3.5l5-1.75Z" />
    <path d="m6 6 4 4M10 6l-4 4" />
  </Icon>
);
export const RetryIcon = (p: P) => (
  <Icon {...p}>
    <path d="M13 8a5 5 0 1 1-1.6-3.66" />
    <path d="M13 2.5v2.75h-2.75" />
  </Icon>
);
export const RuleIcon = (p: P) => (
  <Icon {...p}>
    <path d="M2.5 3.5h11L9.5 8.4v3.85l-3 1.25V8.4L2.5 3.5Z" />
  </Icon>
);

/** Brand mark: a hovering bird reduced to one shape. */
export function BrandMark({ size = 24 }: { size?: number }) {
  return (
    <svg
      className="brand-mark"
      width={size}
      height={size}
      viewBox="0 0 32 32"
      aria-hidden="true"
      focusable="false"
    >
      <rect width="32" height="32" rx="9" fill="var(--accent)" />
      <path d="M8 21 16 9l8 12-8-4.2L8 21Z" fill="var(--on-accent)" />
    </svg>
  );
}
export const SidebarIcon = (p: P) => (
  <Icon {...p}>
    <rect x="2" y="3" width="12" height="10" rx="2.25" />
    <path d="M6.25 3v10" />
  </Icon>
);
export const CloseIcon = (p: P) => (
  <Icon {...p}>
    <path d="m4 4 8 8M12 4l-8 8" />
  </Icon>
);
export const TrashIcon = (p: P) => (
  <Icon {...p}>
    <path d="M2.75 4.25h10.5M6.25 4.25V3a.75.75 0 0 1 .75-.75h2a.75.75 0 0 1 .75.75v1.25M4.25 4.25l.5 8.1c.03.5.45.9.95.9h4.6c.5 0 .92-.4.95-.9l.5-8.1" />
  </Icon>
);
