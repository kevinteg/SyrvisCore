import { useEffect, useRef, useState, type ButtonHTMLAttributes, type ReactNode } from "react";
import { MoreHorizontal } from "lucide-react";

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 p-6 text-sm text-slate-400">
      <span className="h-4 w-4 animate-spin rounded-full border-2 border-base-600 border-t-accent" />
      {label ?? "Loading…"}
    </div>
  );
}

export function ErrorNote({ error }: { error: Error | string }) {
  return (
    <div className="rounded-lg border border-rose-500/30 bg-rose-500/10 p-4 text-sm text-rose-300">
      {typeof error === "string" ? error : error.message}
    </div>
  );
}

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <div className={`rounded-xl border border-base-700 bg-base-800 ${className}`}>{children}</div>
  );
}

type Variant = "default" | "danger" | "ghost";

const VARIANTS: Record<Variant, string> = {
  default: "bg-base-700 hover:bg-base-600 text-slate-100",
  danger: "bg-rose-600/80 hover:bg-rose-600 text-white",
  ghost: "bg-transparent hover:bg-base-700 text-slate-300",
};

export function Button({
  children,
  variant = "default",
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return (
    <button
      {...props}
      className={`inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs font-medium transition disabled:cursor-not-allowed disabled:opacity-40 ${VARIANTS[variant]} ${className}`}
    >
      {children}
    </button>
  );
}

// A single entry in an ActionMenu. `action` runs a callback; `link` opens a URL
// in a new tab; `note` is non-interactive explanatory text (e.g. "mutations
// disabled"). Callers build the array conditionally, so an action that isn't
// permitted is simply absent — never a dead/failing button.
export type MenuItem =
  | { kind: "action"; label: string; icon?: ReactNode; onClick: () => void; danger?: boolean; disabled?: boolean }
  | { kind: "link"; label: string; icon?: ReactNode; href: string }
  | { kind: "note"; label: string };

// A "…" kebab dropdown of row actions. Closes on outside-click / Escape.
export function ActionMenu({
  items,
  disabled,
  label = "Actions",
}: {
  items: MenuItem[];
  disabled?: boolean;
  label?: string;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function onDoc(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    function onEsc(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onEsc);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onEsc);
    };
  }, [open]);

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={open}
        disabled={disabled}
        onClick={() => setOpen((o) => !o)}
        className="inline-flex items-center justify-center rounded-lg px-2 py-1.5 text-slate-300 transition hover:bg-base-700 disabled:cursor-not-allowed disabled:opacity-40"
      >
        <MoreHorizontal size={16} />
      </button>
      {open && (
        <div
          role="menu"
          className="absolute right-0 z-20 mt-1 w-48 overflow-hidden rounded-lg border border-base-600 bg-base-800 py-1 shadow-xl"
        >
          {items.map((it, i) =>
            it.kind === "note" ? (
              <div key={i} className="px-3 py-1.5 text-[11px] italic text-slate-500">
                {it.label}
              </div>
            ) : it.kind === "link" ? (
              <a
                key={i}
                href={it.href}
                target="_blank"
                rel="noreferrer"
                role="menuitem"
                onClick={() => setOpen(false)}
                className="flex items-center gap-2 px-3 py-1.5 text-xs text-slate-200 hover:bg-base-700"
              >
                {it.icon}
                {it.label}
              </a>
            ) : (
              <button
                key={i}
                type="button"
                role="menuitem"
                disabled={it.disabled}
                onClick={() => {
                  setOpen(false);
                  it.onClick();
                }}
                className={`flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs transition hover:bg-base-700 disabled:cursor-not-allowed disabled:opacity-40 ${
                  it.danger ? "text-rose-300" : "text-slate-200"
                }`}
              >
                {it.icon}
                {it.label}
              </button>
            ),
          )}
        </div>
      )}
    </div>
  );
}
