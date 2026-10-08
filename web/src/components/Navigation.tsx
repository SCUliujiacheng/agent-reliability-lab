interface NavigationProps {
  onNavigate?: (anchor: string) => void;
}

const NAV_ITEMS = [
  { label: "Runs", href: "#runs" },
  { label: "Scenarios", href: "#scenarios" },
  { label: "Evaluations", href: "#evaluations" },
] as const;

export function Navigation({ onNavigate }: NavigationProps) {
  return (
    <header className="site-header">
      <a
        className="brand"
        href="#overview"
        aria-label="Agent Reliability Lab home"
        onClick={(event) => {
          if (!onNavigate) return;
          event.preventDefault();
          onNavigate("#overview");
        }}
      >
        Agent Reliability Lab
      </a>
      <nav aria-label="Primary navigation">
        <ul className="nav-list">
          {NAV_ITEMS.map((item) => (
            <li key={item.href}>
              <a
                className="nav-link"
                href={item.href}
                onClick={(event) => {
                  if (!onNavigate) return;
                  event.preventDefault();
                  onNavigate(item.href);
                }}
              >
                {item.label}
              </a>
            </li>
          ))}
        </ul>
      </nav>
    </header>
  );
}
