import { useMemo, useState } from "react";
import { formatBytes } from "../api.ts";
type TreeFile = { path: string; size?: number };

type TreeNode = {
  name: string;
  path: string;
  kind: "directory" | "file";
  children: TreeNode[];
  fileCount: number;
  totalSize?: number;
};

function buildTree(files: TreeFile[]): TreeNode[] {
  type MutableNode = { name: string; path: string; children: Map<string, MutableNode>; file?: TreeFile };
  const root = new Map<string, MutableNode>();
  for (const file of files) {
    let children = root;
    let path = "";
    for (const [index, name] of file.path.split("/").filter(Boolean).entries()) {
      path = path ? `${path}/${name}` : name;
      let node = children.get(name);
      if (!node) {
        node = { name, path, children: new Map() };
        children.set(name, node);
      }
      if (index === file.path.split("/").filter(Boolean).length - 1) node.file = file;
      children = node.children;
    }
  }
  const finalize = (nodes: Map<string, MutableNode>): TreeNode[] => [...nodes.values()]
    .map((node) => {
      const children = finalize(node.children);
      return {
        name: node.name,
        path: node.path,
        kind: children.length > 0 ? "directory" as const : "file" as const,
        children,
        fileCount: node.file ? 1 : children.reduce((total, child) => total + child.fileCount, 0),
        totalSize: node.file
          ? node.file.size
          : children.every((child) => child.totalSize !== undefined)
            ? children.reduce((total, child) => total + (child.totalSize ?? 0), 0)
            : undefined,
      };
    })
    .sort((left, right) => left.kind === right.kind
      ? left.name.localeCompare(right.name)
      : left.kind === "directory" ? -1 : 1);
  return finalize(root);
}

function filterTree(nodes: TreeNode[], query: string): TreeNode[] {
  if (!query) return nodes;
  return nodes.flatMap((node) => {
    const children = filterTree(node.children, query);
    return node.path.toLocaleLowerCase().includes(query) || children.length > 0
      ? [{ ...node, children }]
      : [];
  });
}

function Rows({ nodes, expanded, searching, onToggle }: {
  nodes: TreeNode[];
  expanded: Set<string>;
  searching: boolean;
  onToggle: (path: string) => void;
}) {
  return <>{nodes.map((node) => {
    const open = node.kind === "directory" && (searching || expanded.has(node.path));
    return <div className="file-tree-node" key={node.path}>
      <div className="file-tree-row artifact-file-tree-row">
        {node.kind === "directory"
          ? <button type="button" className="file-tree-toggle" aria-label={`${open ? "Collapse" : "Expand"} ${node.path}`} aria-expanded={open} onClick={() => onToggle(node.path)}>{open ? "−" : "+"}</button>
          : <span className="file-tree-toggle" aria-hidden="true" />}
        <span className="artifact-file-tree-label">
          <code title={node.path}>{node.name}{node.kind === "directory" ? "/" : ""}</code>
          {(node.kind === "directory" || node.totalSize !== undefined) && <small>
            {node.kind === "directory" ? `${node.fileCount.toLocaleString()} files` : ""}
            {node.totalSize === undefined ? "" : `${node.kind === "directory" ? " · " : ""}${formatBytes(node.totalSize)}`}
          </small>}
        </span>
      </div>
      {open && node.children.length > 0 && <div className="file-tree-children"><Rows nodes={node.children} expanded={expanded} searching={searching} onToggle={onToggle} /></div>}
    </div>;
  })}</>;
}

export function ArtifactFileTree({ files, title = "Files" }: { files: TreeFile[]; title?: string }) {
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const tree = useMemo(() => buildTree(files), [files]);
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const visible = useMemo(() => filterTree(tree, normalizedQuery), [tree, normalizedQuery]);
  function toggle(path: string) {
    setExpanded((current) => {
      const next = new Set(current);
      next.has(path) ? next.delete(path) : next.add(path);
      return next;
    });
  }
  return <section className="artifact-file-tree">
    <div className="artifact-file-tree-head"><div><span>{title}</span><strong>{files.length.toLocaleString()} files</strong></div><input type="search" value={query} placeholder="Filter file tree" aria-label={`Filter ${title.toLocaleLowerCase()}`} onChange={(event) => setQuery(event.target.value)} /></div>
    <div className="file-selection-list artifact-file-tree-list"><Rows nodes={visible} expanded={expanded} searching={normalizedQuery.length > 0} onToggle={toggle} />{visible.length === 0 && <p className="muted">No files match this filter.</p>}</div>
  </section>;
}
