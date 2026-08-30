import { useMemo, useState } from "react";
import { formatBytes } from "../api.ts";

export type FileTreeFile = { path: string; size?: number };

type FileTreeNode = {
  name: string;
  path: string;
  kind: "directory" | "file";
  children: FileTreeNode[];
  filePaths: string[];
  totalSize?: number;
};

function buildTree(files: FileTreeFile[]): FileTreeNode[] {
  type MutableNode = {
    name: string;
    path: string;
    children: Map<string, MutableNode>;
    file?: FileTreeFile;
  };
  const root = new Map<string, MutableNode>();
  for (const file of files) {
    const parts = file.path.split("/").filter(Boolean);
    let children = root;
    let path = "";
    for (const [index, name] of parts.entries()) {
      path = path ? `${path}/${name}` : name;
      let node = children.get(name);
      if (!node) {
        node = { name, path, children: new Map() };
        children.set(name, node);
      }
      if (index === parts.length - 1) node.file = file;
      children = node.children;
    }
  }

  function finalize(nodes: Map<string, MutableNode>): FileTreeNode[] {
    return [...nodes.values()].map((node) => {
      const children = finalize(node.children);
      const ownFiles = node.file ? [node.file] : [];
      const filePaths = [
        ...ownFiles.map((file) => file.path),
        ...children.flatMap((child) => child.filePaths),
      ];
      const sizes = [
        ...ownFiles.map((file) => file.size),
        ...children.map((child) => child.totalSize),
      ];
      return {
        name: node.name,
        path: node.path,
        kind: children.length > 0 ? "directory" as const : "file" as const,
        children,
        filePaths,
        totalSize: sizes.every((size) => size !== undefined)
          ? sizes.reduce<number>((total, size) => total + (size ?? 0), 0)
          : undefined,
      };
    }).sort((left, right) => left.kind === right.kind
      ? left.name.localeCompare(right.name)
      : left.kind === "directory" ? -1 : 1);
  }

  return finalize(root);
}

function filterTree(nodes: FileTreeNode[], query: string): FileTreeNode[] {
  if (!query) return nodes;
  return nodes.flatMap((node) => {
    const children = filterTree(node.children, query);
    return node.path.toLocaleLowerCase().includes(query) || children.length > 0
      ? [{ ...node, children }]
      : [];
  });
}

function Rows({
  nodes,
  expanded,
  searching,
  selected,
  onSelectionChange,
  onExpand,
}: {
  nodes: FileTreeNode[];
  expanded: Set<string>;
  searching: boolean;
  selected?: Set<string>;
  onSelectionChange?: (paths: string[], checked: boolean) => void;
  onExpand: (path: string) => void;
}) {
  const selectable = selected !== undefined && onSelectionChange !== undefined;
  return <>{nodes.map((node) => {
    const open = node.kind === "directory" && (searching || expanded.has(node.path));
    const selectedCount = selectable
      ? node.filePaths.filter((path) => selected.has(path)).length
      : 0;
    const checked = selectable && selectedCount === node.filePaths.length;
    const partiallyChecked = selectable && selectedCount > 0 && !checked;
    const detail = node.kind === "directory"
      ? `${node.filePaths.length.toLocaleString()} files`
      : "file";
    return <div className="file-tree-node" key={node.path}>
      <div className="file-tree-row">
        {node.kind === "directory"
          ? <button type="button" className="file-tree-toggle" aria-label={`${open ? "Collapse" : "Expand"} ${node.path}`} aria-expanded={open} onClick={() => onExpand(node.path)}>{open ? "−" : "+"}</button>
          : <span className="file-tree-toggle" aria-hidden="true" />}
        {selectable
          ? <label className="file-tree-label">
            <input type="checkbox" checked={checked} ref={(element) => { if (element) element.indeterminate = partiallyChecked; }} onChange={(event) => onSelectionChange(node.filePaths, event.target.checked)} />
            <span><code title={node.path}>{node.name}{node.kind === "directory" ? "/" : ""}</code><small>{detail}{node.totalSize === undefined ? " · size unavailable" : ` · ${formatBytes(node.totalSize)}`}</small></span>
          </label>
          : <span className="file-tree-readonly-label">
            <code title={node.path}>{node.name}{node.kind === "directory" ? "/" : ""}</code>
            {(node.kind === "directory" || node.totalSize !== undefined) && <small>{node.kind === "directory" ? detail : ""}{node.totalSize === undefined ? "" : `${node.kind === "directory" ? " · " : ""}${formatBytes(node.totalSize)}`}</small>}
          </span>}
      </div>
      {open && node.children.length > 0 && <div className="file-tree-children"><Rows nodes={node.children} expanded={expanded} searching={searching} selected={selected} onSelectionChange={onSelectionChange} onExpand={onExpand} /></div>}
    </div>;
  })}</>;
}

export function SelectableFileTree({
  files,
  query,
  selected,
  expanded,
  onSelectionChange,
  onExpand,
}: {
  files: FileTreeFile[];
  query: string;
  selected: Set<string>;
  expanded: Set<string>;
  onSelectionChange: (paths: string[], checked: boolean) => void;
  onExpand: (path: string) => void;
}) {
  const tree = useMemo(() => buildTree(files), [files]);
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const visible = useMemo(() => filterTree(tree, normalizedQuery), [tree, normalizedQuery]);
  return <>
    <Rows nodes={visible} expanded={expanded} searching={normalizedQuery.length > 0} selected={selected} onSelectionChange={onSelectionChange} onExpand={onExpand} />
    {visible.length === 0 && <p className="muted">No files match this filter.</p>}
  </>;
}

export function FileTree({ files, title = "Files" }: { files: FileTreeFile[]; title?: string }) {
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
  return <section className="file-tree-panel">
    <div className="file-tree-head"><div><span>{title}</span><strong>{files.length.toLocaleString()} files</strong></div><input type="search" value={query} placeholder="Filter file tree" aria-label={`Filter ${title.toLocaleLowerCase()}`} onChange={(event) => setQuery(event.target.value)} /></div>
    <div className="file-selection-list file-tree-list"><Rows nodes={visible} expanded={expanded} searching={normalizedQuery.length > 0} onExpand={toggle} />{visible.length === 0 && <p className="muted">No files match this filter.</p>}</div>
  </section>;
}
