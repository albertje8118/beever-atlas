import { useEffect, useLayoutEffect, useRef } from "react";
import cytoscape, { type Core, type ElementDefinition } from "cytoscape";
import type { GraphEntity, GraphRelationship } from "@/hooks/useGraph";
import { getTypeColors } from "./GraphFilters";

interface GraphCanvasProps {
  entities: GraphEntity[];
  relationships: GraphRelationship[];
  visibleTypes: string[];
  /** When true, orphan (unconnected) nodes are injected into the canvas */
  showOrphans: boolean;
  onSelectEntity: (id: string | null) => void;
  selectedEntityId: string | null;
  /** Callback so parent can read the current orphan count for the pill */
  onOrphanCount: (count: number) => void;
}

/** Cache of node positions keyed by entity ID for deterministic layout */
const positionCache = new Map<string, { x: number; y: number }>();

/**
 * Build the cytoscape ElementDefinition for a single entity.
 * Obsidian-style: small fixed dot (12 px) with label BELOW.
 * Hub nodes get slightly larger dots (up to 20 px) and bolder labels.
 */
function buildNode(
  e: GraphEntity,
  connectionCount: Map<string, number>,
  filteredIds: Set<string>,
): ElementDefinition {
  const colors = getTypeColors(e.type);
  const conns = connectionCount.get(e.id) ?? 0;
  // Dot size: 12 px base, +1 px per connection, max 22 px — keeps nodes small
  const dotSize = Math.min(22, 12 + conns);
  const cached = positionCache.get(e.id);
  const visualDesc = (e.properties as Record<string, unknown>)?.visual_description as string | undefined;
  const isPending = e.status === "pending";
  // Label font: 9 px base for orphans, slightly larger for hubs
  const fontSize = conns === 0 ? 9 : Math.min(12, 9 + Math.floor(conns / 2));
  return {
    data: {
      id: e.id,
      label: e.name,
      type: e.type,
      bgColor: colors.node,
      borderColor: colors.nodeBorder,
      dotSize,
      fontSize,
      hasMedia: !!visualDesc,
      visualDesc: visualDesc || "",
      pending: isPending,
      isOrphan: !filteredIds.has(e.id) || conns === 0,
    },
    ...(cached ? { position: cached } : {}),
  };
}

export function GraphCanvas({
  entities,
  relationships,
  visibleTypes,
  showOrphans,
  onSelectEntity,
  selectedEntityId,
  onOrphanCount,
}: GraphCanvasProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const cyRef = useRef<Core | null>(null);

  // Stable ref to the orphan entities list so the showOrphans effect can
  // inject/remove without re-mounting cytoscape.
  const orphanEntitiesRef = useRef<GraphEntity[]>([]);

  const onSelectRef = useRef(onSelectEntity);
  useLayoutEffect(() => {
    onSelectRef.current = onSelectEntity;
  });

  const onOrphanCountRef = useRef(onOrphanCount);
  useLayoutEffect(() => {
    onOrphanCountRef.current = onOrphanCount;
  });

  // Main build effect — fires when data or type filter changes.
  // Never adds orphan nodes here; the showOrphans effect handles that.
  useEffect(() => {
    if (!containerRef.current) return;

    const visibleSet = new Set(visibleTypes);
    const filtered = entities
      .filter((e) => visibleSet.has(e.type))
      .slice(0, 80);

    const filteredIds = new Set(filtered.map((e) => e.id));

    // Count connections per visible node
    const connectionCount = new Map<string, number>();
    relationships.forEach((r) => {
      if (filteredIds.has(r.source_id) && filteredIds.has(r.target_id)) {
        connectionCount.set(r.source_id, (connectionCount.get(r.source_id) ?? 0) + 1);
        connectionCount.set(r.target_id, (connectionCount.get(r.target_id) ?? 0) + 1);
      }
    });

    // Connected vs isolated split
    const connectedFiltered = filtered.filter(
      (e) => (connectionCount.get(e.id) ?? 0) > 0,
    );
    const isolatedEntities = filtered.filter(
      (e) => (connectionCount.get(e.id) ?? 0) === 0,
    );
    // Degenerate: if nothing is connected, render everything
    const renderableEntities =
      connectedFiltered.length > 0 ? connectedFiltered : filtered;
    const orphans = connectedFiltered.length > 0 ? isolatedEntities : [];
    orphanEntitiesRef.current = orphans;
    onOrphanCountRef.current(orphans.length);

    const hasCachedPositions = renderableEntities.some((e) => positionCache.has(e.id));

    const nodes: ElementDefinition[] = renderableEntities.map((e) =>
      buildNode(e, connectionCount, filteredIds),
    );

    const edges: ElementDefinition[] = relationships
      .filter((r) => filteredIds.has(r.source_id) && filteredIds.has(r.target_id))
      .map((r, i) => ({
        data: {
          id: r.id || `edge-${i}`,
          source: r.source_id,
          target: r.target_id,
          label: r.type.replace(/_/g, " "),
        },
      }));

    const isDark = document.documentElement.classList.contains("dark");
    const labelColor = isDark ? "#e2e8f0" : "#1e293b";
    const edgeLabelColor = isDark ? "#94a3b8" : "#64748b";
    const edgeLabelBg = isDark ? "#1e293b" : "#f8fafc";
    const edgeLineColor = isDark ? "#334155" : "#cbd5e1";
    const edgeHoverColor = isDark ? "#94a3b8" : "#64748b";
    const edgeHoverLabelColor = isDark ? "#e2e8f0" : "#334155";
    const edgeHighlightColor = isDark ? "#38bdf8" : "#0B4F6C";

    if (cyRef.current) {
      cyRef.current.destroy();
    }

    const cy = cytoscape({
      container: containerRef.current,
      elements: [...nodes, ...edges],
      style: [
        // ─── Node: Obsidian small-dot style ───────────────────────────
        // Label is BELOW the node (text-valign: bottom, text-margin-y pushes
        // it further down). No text inside the disk. Dot is small + crisp.
        {
          selector: "node",
          style: {
            "background-color": "data(bgColor)",
            "border-color": "data(borderColor)",
            "border-width": 1.5,
            // Label sits below the node, not inside
            label: "data(label)",
            color: labelColor,
            "font-size": "data(fontSize)",
            "font-weight": 500,
            "text-valign": "bottom",
            "text-halign": "center",
            // Push the label 6 px below the dot's edge so it reads as separate
            "text-margin-y": 6,
            "text-wrap": "wrap",
            // Allow 2 lines for long snake_case names
            "text-max-width": "90px",
            // No outline — label is outside the colored disk so no clash
            "text-outline-width": 0,
            width: "data(dotSize)",
            height: "data(dotSize)",
            opacity: 1,
            "transition-property": "border-width, border-color, width, height, opacity, background-color",
            "transition-duration": "0.2s",
          } as unknown as cytoscape.Css.Node,
        },
        // Orphan nodes (injected when showOrphans=true) appear slightly dimmer
        {
          selector: "node[?isOrphan]",
          style: {
            opacity: 0.65,
          },
        },
        {
          selector: "node.selected-highlight",
          style: {
            "border-width": 3,
            "border-color": "#ffffff",
            "overlay-color": "#0B4F6C",
            "overlay-opacity": 0.15,
          },
        },
        {
          selector: "node.hover",
          style: {
            "border-width": 2.5,
            "border-color": "#ffffff",
            "overlay-color": "#0B4F6C",
            "overlay-opacity": 0.1,
          },
        },
        {
          selector: "node[?hasMedia]",
          style: {
            "border-style": "double" as const,
            "border-width": 3,
          },
        },
        {
          selector: "node[?pending]",
          style: {
            "border-style": "dashed" as const,
            opacity: 0.45,
          },
        },
        {
          selector: "node.dimmed",
          style: { opacity: 0.2 },
        },
        {
          selector: "node.neighbor",
          style: {
            "border-width": 2.5,
            "border-color": "#facc15",
          },
        },
        // ─── Edge styles ──────────────────────────────────────────────
        {
          selector: "edge",
          style: {
            width: 1,
            "line-color": edgeLineColor,
            "target-arrow-color": edgeLineColor,
            "target-arrow-shape": "triangle",
            "arrow-scale": 0.6,
            "curve-style": "bezier",
            label: "data(label)",
            "font-size": "7px",
            color: edgeLabelColor,
            "text-rotation": "autorotate",
            "text-margin-y": -5,
            "text-background-color": edgeLabelBg,
            "text-background-opacity": 0.85,
            "text-background-padding": "2px",
            "line-style": "solid",
            opacity: 0.55,
            "transition-property": "width, line-color, opacity",
            "transition-duration": "0.2s",
          } as unknown as cytoscape.Css.Edge,
        },
        {
          selector: "edge.hover",
          style: {
            width: 2,
            "line-color": edgeHoverColor,
            "target-arrow-color": edgeHoverColor,
            "font-size": "8px",
            color: edgeHoverLabelColor,
            opacity: 1,
          },
        },
        {
          selector: "edge.dimmed",
          style: { opacity: 0.12 },
        },
        {
          selector: "edge.highlighted",
          style: {
            width: 2,
            "line-color": edgeHighlightColor,
            "target-arrow-color": edgeHighlightColor,
            "font-size": "8px",
            color: edgeHighlightColor,
            opacity: 1,
          },
        },
      ],
      layout: hasCachedPositions
        ? { name: "preset", fit: true, padding: 60 }
        : {
            // cose with safe spread values. nodeRepulsion 80k–100k is the
            // confirmed safe range. 200k caused blank canvas (cose fit zoomed
            // past minZoom). idealEdgeLength 220 gives Obsidian-style generous
            // spacing between connected clusters. gravity 0.15 keeps graph
            // from collapsing without drifting off-canvas.
            name: "cose",
            animate: "end",
            animationDuration: 700,
            animationEasing: "ease-out-cubic" as cytoscape.Css.TransitionTimingFunction,
            randomize: false,
            // nodeDimensionsIncludeLabels=true is critical for Obsidian style:
            // labels are BELOW nodes so they need layout clearance to avoid
            // overlapping adjacent labels.
            nodeDimensionsIncludeLabels: true,
            nodeRepulsion: () => 100000,
            idealEdgeLength: () => 220,
            edgeElasticity: () => 45,
            gravity: 0.15,
            padding: 100,
            fit: true,
          } as cytoscape.LayoutOptions,
      minZoom: 0.2,
      maxZoom: 3,
      wheelSensitivity: 0.3,
    });

    // Save positions after layout
    if (!hasCachedPositions) {
      cy.one("layoutstop", () => {
        cy.nodes().forEach((node) => {
          const pos = node.position();
          positionCache.set(node.id(), { x: pos.x, y: pos.y });
        });
      });
    }

    // ─── Physics: spring drag + momentum ─────────────────────────────
    let dragTarget: cytoscape.NodeSingular | null = null;
    const velocities = new Map<string, { vx: number; vy: number }>();
    let momentumFrame: number | null = null;

    cy.on("grab", "node", (evt) => {
      dragTarget = evt.target;
      velocities.clear();
      if (momentumFrame) {
        cancelAnimationFrame(momentumFrame);
        momentumFrame = null;
      }
    });

    cy.on("drag", "node", () => {
      if (!dragTarget) return;
      const pos = dragTarget.position();
      dragTarget.neighborhood("node").forEach((neighbor) => {
        const nPos = neighbor.position();
        const dx = pos.x - nPos.x;
        const dy = pos.y - nPos.y;
        const dist = Math.sqrt(dx * dx + dy * dy);
        if (dist < 40) return;
        const force = Math.min(0.02, 80 / (dist * dist));
        const moveX = dx * force;
        const moveY = dy * force;
        neighbor.position({ x: nPos.x + moveX, y: nPos.y + moveY });
        const prev = velocities.get(neighbor.id()) || { vx: 0, vy: 0 };
        velocities.set(neighbor.id(), {
          vx: prev.vx * 0.5 + moveX * 8,
          vy: prev.vy * 0.5 + moveY * 8,
        });
      });
    });

    cy.on("free", "node", () => {
      dragTarget = null;
      const friction = 0.88;
      const minSpeed = 0.3;
      const step = () => {
        let anyMoving = false;
        velocities.forEach((vel, nodeId) => {
          if (Math.abs(vel.vx) < minSpeed && Math.abs(vel.vy) < minSpeed) return;
          const node = cy.getElementById(nodeId);
          if (!node.length) return;
          const p = node.position();
          node.position({ x: p.x + vel.vx, y: p.y + vel.vy });
          vel.vx *= friction;
          vel.vy *= friction;
          anyMoving = true;
        });
        if (anyMoving) {
          momentumFrame = requestAnimationFrame(step);
        } else {
          momentumFrame = null;
          cy.nodes().forEach((n) => {
            positionCache.set(n.id(), { ...n.position() });
          });
        }
      };
      momentumFrame = requestAnimationFrame(step);
      cy.nodes().forEach((n) => {
        positionCache.set(n.id(), { ...n.position() });
      });
    });

    // ─── Interactions ─────────────────────────────────────────────────
    let tooltip: HTMLDivElement | null = null;

    cy.on("mouseover", "node", (evt) => {
      evt.target.addClass("hover");
      const node = evt.target;
      const type = node.data("type") as string;
      const label = node.data("label") as string;
      // Hover float: grow dot by ~30% (small dot so 30% is still subtle)
      const baseSize = node.data("dotSize") as number;
      node.stop(true, false).animate(
        { style: { width: baseSize * 1.3, height: baseSize * 1.3 } },
        { duration: 150, easing: "ease-out-cubic" as cytoscape.Css.TransitionTimingFunction },
      );

      if (!tooltip) {
        tooltip = document.createElement("div");
        tooltip.style.cssText =
          "position:absolute;pointer-events:none;z-index:50;padding:5px 10px;" +
          "border-radius:6px;font-size:11px;white-space:nowrap;" +
          "background:rgba(15,23,42,0.92);color:#f1f5f9;box-shadow:0 2px 12px rgba(0,0,0,0.25);" +
          "border:1px solid rgba(255,255,255,0.08);backdrop-filter:blur(4px);";
        containerRef.current?.appendChild(tooltip);
      }
      const visualDesc = node.data("visualDesc") as string | undefined;
      if (visualDesc) {
        tooltip.textContent = `${label} · ${type}\n${visualDesc.slice(0, 120)}`;
        tooltip.style.whiteSpace = "pre-wrap";
        tooltip.style.maxWidth = "280px";
      } else {
        tooltip.textContent = `${label} · ${type}`;
        tooltip.style.whiteSpace = "nowrap";
        tooltip.style.maxWidth = "";
      }
      tooltip.style.display = "block";
    });

    cy.on("mousemove", "node", (evt) => {
      if (tooltip && containerRef.current) {
        const rect = containerRef.current.getBoundingClientRect();
        tooltip.style.left = `${evt.originalEvent.clientX - rect.left + 14}px`;
        tooltip.style.top = `${evt.originalEvent.clientY - rect.top - 32}px`;
      }
    });

    cy.on("mouseout", "node", (evt) => {
      const node = evt.target;
      node.removeClass("hover");
      const baseSize = node.data("dotSize") as number;
      node.stop(true, false).animate(
        { style: { width: baseSize, height: baseSize } },
        { duration: 130, easing: "ease-in-cubic" as cytoscape.Css.TransitionTimingFunction },
      );
      if (tooltip) tooltip.style.display = "none";
    });

    cy.on("tap", "node", (evt) => {
      const node = evt.target;
      const neighborhood = node.closedNeighborhood();
      cy.elements().removeClass("dimmed highlighted hover neighbor");
      cy.elements().addClass("dimmed");
      neighborhood.removeClass("dimmed");
      neighborhood.nodes().addClass("neighbor");
      neighborhood.edges().addClass("highlighted");
      onSelectRef.current(node.id());
    });

    cy.on("tap", (evt) => {
      if (evt.target === cy) {
        cy.elements().removeClass("dimmed highlighted hover neighbor");
        onSelectRef.current(null);
      }
    });

    cy.on("dbltap", "node", (evt) => {
      const neighborhood = evt.target.closedNeighborhood();
      cy.animate({
        fit: { eles: neighborhood, padding: 80 },
        duration: 500,
        easing: "ease-in-out-cubic" as cytoscape.Css.TransitionTimingFunction,
      });
    });

    cyRef.current = cy;

    return () => {
      if (momentumFrame) cancelAnimationFrame(momentumFrame);
      if (tooltip) tooltip.remove();
      cy.destroy();
      cyRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entities, relationships, visibleTypes]);

  // ─── Show / hide orphan nodes without remounting cytoscape ──────────
  // When showOrphans flips true we cy.add() the orphan nodes with preset
  // positions placed in a sparse column to the right of the main graph.
  // When it flips false we remove them by class "orphan-node".
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;

    if (!showOrphans) {
      // Remove any previously injected orphan nodes
      cy.remove("node.orphan-node");
      return;
    }

    const orphans = orphanEntitiesRef.current;
    if (orphans.length === 0) return;

    // Find the bounding box of existing nodes to place orphans to the right
    const bbox = cy.nodes().boundingBox({});
    const startX = (bbox.x2 ?? 400) + 160;
    const startY = bbox.y1 ?? 0;
    const stepY = 80;

    // Build visible connection count context (orphans have 0 connections by definition)
    const emptyCount = new Map<string, number>();
    const emptyIds = new Set<string>();

    const newElements: ElementDefinition[] = orphans.map((e, i) => {
      const node = buildNode(e, emptyCount, emptyIds);
      const cachedPos = positionCache.get(e.id);
      return {
        ...node,
        position: cachedPos ?? { x: startX, y: startY + i * stepY },
        classes: "orphan-node",
      };
    });

    cy.add(newElements);

    // Save positions for these new nodes
    orphans.forEach((e, i) => {
      if (!positionCache.has(e.id)) {
        positionCache.set(e.id, { x: startX, y: startY + i * stepY });
      }
    });

    // Animate in: start transparent, fade to 0.65
    cy.nodes(".orphan-node").style("opacity", 0);
    cy.nodes(".orphan-node").animate(
      { style: { opacity: 0.65 } },
      { duration: 300, easing: "ease-out-cubic" as cytoscape.Css.TransitionTimingFunction },
    );
  }, [showOrphans]);

  // Highlight selected node externally
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.elements().removeClass("dimmed highlighted");
    if (selectedEntityId) {
      const node = cy.getElementById(selectedEntityId);
      if (node.length) {
        const neighborhood = node.closedNeighborhood();
        cy.elements().addClass("dimmed");
        neighborhood.removeClass("dimmed");
        neighborhood.edges().addClass("highlighted");
      }
    }
  }, [selectedEntityId]);

  if (entities.length === 0) {
    return (
      <div className="flex-1 flex items-center justify-center bg-muted/5">
        <div className="text-center space-y-2">
          <div className="text-4xl opacity-20">&#x1f578;&#xfe0f;</div>
          <p className="text-sm text-muted-foreground">
            No entities to display. Run a sync to populate the graph.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      className="flex-1 min-h-0 bg-muted/5 overflow-hidden"
    />
  );
}
