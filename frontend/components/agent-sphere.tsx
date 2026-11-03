import { Icon } from "@/components/icon";

type AgentSphereProps = { onClick: () => void; hidden?: boolean };

export function AgentSphere({ onClick, hidden = false }: AgentSphereProps) {
  return (
    <button
      className={`agent-sphere${hidden ? " agent-sphere--hidden" : ""}`}
      type="button"
      onClick={onClick}
      aria-label="Открыть чат с ИИ-агентом"
      aria-hidden={hidden}
      tabIndex={hidden ? -1 : 0}
    >
      <span className="agent-sphere__art" aria-hidden="true">
        <span className="agent-sphere__orb-art"><img src="/figma/sphere-orb.svg" width="100" height="100" alt="" draggable="false" /></span>
        <span className="agent-sphere__bubble"><Icon name="dots" size={18} /></span>
        <span className="agent-sphere__glint"><img src="/figma/sphere-glint.svg" width="25" height="17" alt="" draggable="false" /></span>
        <span className="agent-sphere__tail"><img src="/figma/sphere-tail.svg" width="8" height="8" alt="" draggable="false" /></span>
      </span>
      <span className="agent-sphere__sr-only">Чат с агентом</span>
    </button>
  );
}
