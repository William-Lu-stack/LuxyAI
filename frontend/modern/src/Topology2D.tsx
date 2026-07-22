import { useEffect, useMemo, useState } from "react";
import dagre from "@dagrejs/dagre";
import {
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  applyNodeChanges,
  type Edge,
  type Node,
  type NodeProps,
  type ReactFlowInstance,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

export type FlowTopologyNode = {
  id: string;
  name: string;
  title: string;
  type: string;
  kind: string;
  cluster: string;
  namespace: string;
  risk?: string;
};

export type FlowTopologyEdge = {
  source: string;
  target: string;
  type: string;
  traffic?: string;
};

type TopologyNodeData = {
  label: string;
  type: string;
  cluster: string;
  namespace: string;
  risk: string;
  focused?: boolean;
  degree?: number;
};

const NODE_WIDTH = 208;
const NODE_HEIGHT = 74;

function nodeTone(type: string, risk: string) {
  if (/critical|high|failed|error/i.test(risk)) return "risk";
  if (/kafka|redis|mysql|elastic|data|storage/i.test(type)) return "data";
  if (/service|ingress|gateway/i.test(type)) return "service";
  if (/pod|container/i.test(type)) return "pod";
  if (/node|infra/i.test(type)) return "infra";
  return "workload";
}

function TopologyNodeCard({ data, selected }: NodeProps<Node<TopologyNodeData>>) {
  const tone = nodeTone(data.type, data.risk);
  return (
    <div className={`flow-node ${tone} ${selected ? "selected" : ""} ${data.focused === false ? "dimmed" : ""}`}>
      <Handle type="target" position={Position.Left} />
      <span className="flow-node-dot" />
      <div>
        <small>{data.type || "resource"}{data.degree ? ` · ${data.degree} links` : ""}</small>
        <strong title={data.label}>{data.label}</strong>
        <p>{data.cluster}{data.namespace ? ` / ${data.namespace}` : ""}</p>
      </div>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

const nodeTypes = { topology: TopologyNodeCard };

function buildFocusSet(edges: FlowTopologyEdge[], selectedId: string) {
  if (!selectedId) return null;
  const focus = new Set([selectedId]);
  edges.forEach((edge) => {
    if (edge.source === selectedId) focus.add(edge.target);
    if (edge.target === selectedId) focus.add(edge.source);
  });
  edges.forEach((edge) => {
    if (focus.has(edge.source) || focus.has(edge.target)) {
      focus.add(edge.source);
      focus.add(edge.target);
    }
  });
  return focus;
}

function simplifyReplicaPods(nodes: FlowTopologyNode[], edges: FlowTopologyEdge[], selectedId: string) {
  const nodeById = new Map(nodes.map((node) => [node.id, node]));
  const degree = new Map<string, number>();
  edges.forEach((edge) => {
    degree.set(edge.source, (degree.get(edge.source) || 0) + 1);
    degree.set(edge.target, (degree.get(edge.target) || 0) + 1);
  });
  const groups = new Map<string, FlowTopologyNode[]>();
  edges.forEach((edge) => {
    const target = nodeById.get(edge.target);
    if (!target) return;
    const type = `${target.type} ${target.kind}`.toLowerCase();
    const safeReplica = type.includes("pod") && !/critical|high|failed|error/i.test(String(target.risk || ""));
    const ownedOnly = (degree.get(target.id) || 0) === 1 && /own|contain|replica/i.test(edge.type || "");
    if (!safeReplica || !ownedOnly || target.id === selectedId) return;
    const bucket = groups.get(edge.source) || [];
    bucket.push(target);
    groups.set(edge.source, bucket);
  });

  const collapsedIds = new Set<string>();
  const collapsedNodes: FlowTopologyNode[] = [];
  const collapsedEdges: FlowTopologyEdge[] = [];
  groups.forEach((members, source) => {
    if (members.length < 9) return;
    members.forEach((member) => collapsedIds.add(member.id));
    const first = members[0];
    const id = `pod-group:${source}`;
    collapsedNodes.push({
      id,
      name: `${members.length} ready pods`,
      title: `${members.length} normal Pods`,
      type: "pod",
      kind: "PodGroup",
      cluster: first.cluster,
      namespace: first.namespace,
      risk: "normal",
    });
    collapsedEdges.push({ source, target: id, type: `owns ${members.length} pods` });
  });
  if (!collapsedIds.size) return { nodes, edges };
  return {
    nodes: nodes.filter((node) => !collapsedIds.has(node.id)).concat(collapsedNodes),
    edges: edges.filter((edge) => !collapsedIds.has(edge.source) && !collapsedIds.has(edge.target)).concat(collapsedEdges),
  };
}

function layoutGraph(nodes: FlowTopologyNode[], edges: FlowTopologyEdge[], selectedId: string) {
  const simplified = simplifyReplicaPods(nodes, edges, selectedId);
  nodes = simplified.nodes;
  edges = simplified.edges;
  const graph = new dagre.graphlib.Graph();
  graph.setDefaultEdgeLabel(() => ({}));
  const nodeCount = nodes.length;
  graph.setGraph({
    rankdir: "LR",
    ranksep: nodeCount > 180 ? 112 : nodeCount > 80 ? 132 : 156,
    nodesep: nodeCount > 180 ? 46 : nodeCount > 80 ? 58 : 72,
    edgesep: 24,
    marginx: 54,
    marginy: 54,
    acyclicer: "greedy",
    ranker: "network-simplex",
  });

  const ids = new Set(nodes.map((node) => node.id));
  const validEdges = edges.filter((edge) => ids.has(edge.source) && ids.has(edge.target));
  const focusIds = buildFocusSet(validEdges, selectedId);
  const degree = new Map<string, number>();
  nodes.forEach((node) => graph.setNode(node.id, { width: NODE_WIDTH, height: NODE_HEIGHT }));
  validEdges.forEach((edge) => {
    graph.setEdge(edge.source, edge.target);
    degree.set(edge.source, (degree.get(edge.source) || 0) + 1);
    degree.set(edge.target, (degree.get(edge.target) || 0) + 1);
  });
  dagre.layout(graph);

  const flowNodes: Node<TopologyNodeData>[] = nodes.map((node) => {
    const position = graph.node(node.id) || { x: 0, y: 0 };
    return {
      id: node.id,
      type: "topology",
      position: { x: position.x - NODE_WIDTH / 2, y: position.y - NODE_HEIGHT / 2 },
      data: {
        label: node.title || node.name,
        type: node.type || node.kind,
        cluster: node.cluster,
        namespace: node.namespace,
        risk: String(node.risk || "normal"),
        focused: focusIds ? focusIds.has(node.id) : true,
        degree: degree.get(node.id) || 0,
      },
    };
  });

  const flowEdges: Edge[] = validEdges.map((edge, index) => {
    const dataFlow = /kafka|stream|log|trace|data|event/i.test(edge.type);
    const directlySelected = selectedId && (edge.source === selectedId || edge.target === selectedId);
    const focused = !focusIds || focusIds.has(edge.source) || focusIds.has(edge.target);
    const showLabel = directlySelected || (!focusIds && validEdges.length <= 90) || (focused && validEdges.length <= 180);
    return {
      id: `${edge.source}-${edge.target}-${index}`,
      source: edge.source,
      target: edge.target,
      type: "smoothstep",
      label: showLabel && edge.type !== "dependency" ? edge.type : undefined,
      animated: dataFlow,
      markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16 },
      className: [
        "flow-edge",
        dataFlow ? "data-flow" : "",
        focused ? "" : "dimmed",
        directlySelected ? "focused" : "",
      ].filter(Boolean).join(" "),
      style: { strokeWidth: directlySelected ? 2.8 : dataFlow ? 1.9 : 1.35, opacity: focused ? 1 : 0.18 },
    };
  });

  return { nodes: flowNodes, edges: flowEdges };
}

export function Topology2D({
  nodes,
  edges,
  selectedId,
  onSelect,
  apiRef,
}: {
  nodes: FlowTopologyNode[];
  edges: FlowTopologyEdge[];
  selectedId: string;
  onSelect: (id: string) => void;
  apiRef: React.MutableRefObject<{ reset: () => void; zoom: (factor: number) => void } | null>;
}) {
  const layout = useMemo(() => layoutGraph(nodes, edges, selectedId), [nodes, edges, selectedId]);
  const [flowNodes, setFlowNodes] = useState(layout.nodes);
  const [instance, setInstance] = useState<ReactFlowInstance<Node<TopologyNodeData>, Edge> | null>(null);

  useEffect(() => {
    setFlowNodes(layout.nodes);
    if (instance) requestAnimationFrame(() => instance.fitView({ padding: 0.16, duration: 350, minZoom: 0.54, maxZoom: 1.08 }));
  }, [instance, layout]);

  useEffect(() => {
    if (!instance) return;
    apiRef.current = {
      reset: () => {
        setFlowNodes(layout.nodes);
        instance.fitView({ padding: 0.16, duration: 350, minZoom: 0.54, maxZoom: 1.08 });
      },
      zoom: (factor: number) => {
        instance.zoomTo(Math.max(0.25, Math.min(2.2, instance.getZoom() / factor)), { duration: 180 });
      },
    };
    return () => {
      apiRef.current = null;
    };
  }, [apiRef, instance, layout.nodes]);

  const selectedNodes = useMemo(
    () => flowNodes.map((node) => ({ ...node, selected: node.id === selectedId })),
    [flowNodes, selectedId],
  );

  return (
    <ReactFlow
      nodes={selectedNodes}
      edges={layout.edges}
      nodeTypes={nodeTypes}
      onInit={setInstance}
      onNodeClick={(_, node) => onSelect(node.id)}
      onNodesChange={(changes) => setFlowNodes((current) => applyNodeChanges(changes, current))}
      fitView
      fitViewOptions={{ padding: 0.16, minZoom: 0.54, maxZoom: 1.08 }}
      minZoom={0.18}
      maxZoom={2.4}
      onlyRenderVisibleElements
      elevateEdgesOnSelect
      nodesConnectable={false}
      deleteKeyCode={null}
      proOptions={{ hideAttribution: true }}
    >
      <MiniMap pannable zoomable nodeColor={(node) => {
        const data = node.data as TopologyNodeData;
        const tone = nodeTone(data.type, data.risk);
        return tone === "risk" ? "#dc5b68" : tone === "data" ? "#d99a32" : tone === "pod" ? "#2f9d78" : "#4c76c9";
      }} />
      <Controls showInteractive={false} />
      <Background variant={BackgroundVariant.Dots} gap={22} size={1} />
    </ReactFlow>
  );
}
