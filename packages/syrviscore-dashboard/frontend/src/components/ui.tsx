import type { ButtonHTMLAttributes, ReactNode } from "react";

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
