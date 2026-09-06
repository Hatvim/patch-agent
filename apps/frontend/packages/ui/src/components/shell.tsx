import type { ReactNode } from "react";
import { cn } from "../lib/utils";

export type NavItem = {
  id: string;
  label: string;
  onClick: () => void;
};

type ShellProps = {
  title: string;
  right?: ReactNode;
  children: ReactNode;
  className?: string;
  nav?: NavItem[];
  activeNav?: string;
  logoHref?: string;
  onLogoClick?: () => void;
};

export function Shell({ title, right, children, className, nav, activeNav, logoHref, onLogoClick }: ShellProps) {
  return (
    <div className={cn("min-h-screen w-full bg-[var(--patch-bg)] font-mono text-[var(--patch-fg)]", className)}>
      <div className="mx-auto flex max-w-6xl">
        {nav && nav.length > 0 && (
          <nav className="w-36 shrink-0 border-r border-[var(--patch-border)] px-3 py-6 text-sm">
            {logoHref ? (
              <a
                href={logoHref}
                onClick={(e) => {
                  if (!onLogoClick) return;
                  // Let middle-click, cmd/ctrl-click, etc. follow the native href.
                  if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
                  e.preventDefault();
                  onLogoClick();
                }}
                className="mb-4 block text-[var(--patch-accent)] hover:text-[var(--patch-fg)]"
              >
                ┌─ patch ─
              </a>
            ) : (
              <div className="mb-4 text-[var(--patch-accent)]">┌─ patch ─</div>
            )}
            <ul className="space-y-1">
              {nav.map((item) => {
                const isActive = item.id === activeNav;
                return (
                  <li key={item.id}>
                    <button
                      type="button"
                      onClick={item.onClick}
                      className={cn(
                        "w-full px-1 py-0.5 text-left transition",
                        isActive
                          ? "text-[var(--patch-accent)]"
                          : "text-[var(--patch-dim)] hover:text-[var(--patch-fg)]",
                      )}
                    >
                      [ {item.label} ]
                    </button>
                  </li>
                );
              })}
            </ul>
          </nav>
        )}
        <div className="min-w-0 flex-1 px-4 py-6">
          <div className="mb-4 flex items-center justify-between border-b border-[var(--patch-border)] pb-2 text-sm">
            <span className="text-[var(--patch-accent)]">┌─ {title} ─</span>
            <span className="text-[var(--patch-dim)]">{right}</span>
          </div>
          {children}
        </div>
      </div>
    </div>
  );
}

type RowProps = {
  children: ReactNode;
  right?: ReactNode;
  onClick?: () => void;
  className?: string;
};

export function Row({ children, right, onClick, className }: RowProps) {
  const interactive = Boolean(onClick);
  const Cmp = interactive ? "button" : "div";

  return (
    <Cmp
      type={interactive ? "button" : undefined}
      onClick={onClick}
      className={cn(
        "flex w-full items-center justify-between gap-3 border-b border-[var(--patch-border)] px-2 py-1.5 text-left text-sm",
        interactive && "hover:bg-[var(--patch-surface)] hover:text-[var(--patch-accent)]",
        className,
      )}
    >
      <span className="min-w-0 truncate">{children}</span>
      {right && <span className="shrink-0 text-[var(--patch-dim)]">{right}</span>}
    </Cmp>
  );
}

type Tab = {
  id: string;
  label: string;
};

type TabsProps = {
  tabs: Tab[];
  active: string;
  onChange: (id: string) => void;
};

export function Tabs({ tabs, active, onChange }: TabsProps) {
  return (
    <div className="mb-4 flex gap-1 border-b border-[var(--patch-border)] text-sm">
      {tabs.map((tab) => {
        const isActive = tab.id === active;

        return (
          <button
            key={tab.id}
            type="button"
            onClick={() => onChange(tab.id)}
            className={cn(
              "px-3 py-1.5 transition",
              isActive
                ? "border-b-2 border-[var(--patch-accent)] text-[var(--patch-accent)]"
                : "text-[var(--patch-dim)] hover:text-[var(--patch-fg)]",
            )}
          >
            [ {tab.label} ]
          </button>
        );
      })}
    </div>
  );
}
