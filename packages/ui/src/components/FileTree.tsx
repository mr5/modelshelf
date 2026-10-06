import { ChevronRight, File, Folder, FolderOpen, Search } from "lucide-react";
import { motion, useReducedMotion } from "motion/react";
import { type ReactNode, useMemo, useState } from "react";
import { formatBytes } from "@/api";
import { Checkbox } from "@/components/motion/checkbox";
import {
  FileTree as BeuiFileTree,
  FileTreeFile as BeuiFile,
  FileTreeFolder as BeuiFolder,
} from "@/components/motion/file-tree";
import { Input } from "@/components/motion/input";
import { cn } from "@/lib/utils";

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
    return [...nodes.values()]
      .map((node) => {
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
          kind:
            children.length > 0 ? ("directory" as const) : ("file" as const),
          children,
          filePaths,
          totalSize: sizes.every((size) => size !== undefined)
            ? sizes.reduce<number>((total, size) => total + (size ?? 0), 0)
            : undefined,
        };
      })
      .sort((left, right) =>
        left.kind === right.kind
          ? left.name.localeCompare(right.name)
          : left.kind === "directory"
            ? -1
            : 1,
      );
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
  depth = 0,
}: {
  nodes: FileTreeNode[];
  expanded: Set<string>;
  searching: boolean;
  selected?: Set<string>;
  onSelectionChange?: (paths: string[], checked: boolean) => void;
  onExpand: (path: string) => void;
  depth?: number;
}) {
  const reduce = useReducedMotion();
  const selectable = selected !== undefined && onSelectionChange !== undefined;
  return (
    <>
      {nodes.map((node) => {
        const directory = node.kind === "directory";
        const open = directory && (searching || expanded.has(node.path));
        const selectedCount = selectable
          ? node.filePaths.filter((path) => selected.has(path)).length
          : 0;
        const checked = selectable && selectedCount === node.filePaths.length;
        const partiallyChecked = selectable && selectedCount > 0 && !checked;
        const detail = directory
          ? `${node.filePaths.length.toLocaleString()} ${node.filePaths.length === 1 ? "file" : "files"}`
          : "file";
        const size =
          node.totalSize === undefined
            ? undefined
            : formatBytes(node.totalSize);
        const Icon = directory ? (open ? FolderOpen : Folder) : File;
        return (
          <div
            className="min-w-0 border-t border-border first:border-t-0"
            key={node.path}
          >
            <div
              className="grid min-w-0 grid-cols-[20px_minmax(0,1fr)] items-center gap-1.5 py-2 pr-3 transition-colors hover:bg-muted/70"
              style={{ paddingLeft: 10 + depth * 20 }}
            >
              {directory ? (
                <button
                  type="button"
                  className="grid size-5 place-items-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                  aria-label={`${open ? "Collapse" : "Expand"} ${node.path}`}
                  aria-expanded={open}
                  onClick={() => onExpand(node.path)}
                >
                  <motion.span
                    animate={{ rotate: open ? 90 : 0 }}
                    transition={
                      reduce
                        ? { duration: 0 }
                        : { type: "spring", duration: 0.3, bounce: 0.2 }
                    }
                    className="grid place-items-center"
                  >
                    <ChevronRight className="size-3.5" />
                  </motion.span>
                </button>
              ) : (
                <span aria-hidden />
              )}
              {selectable ? (
                <div className="grid min-w-0 grid-cols-[20px_16px_minmax(0,1fr)] items-center gap-2">
                  <Checkbox
                    checked={checked}
                    indeterminate={partiallyChecked}
                    aria-label={`Select ${node.path}`}
                    onCheckedChange={(next) =>
                      onSelectionChange(
                        node.filePaths,
                        partiallyChecked ? true : next,
                      )
                    }
                  />
                  <Icon
                    className={cn(
                      "size-4",
                      directory ? "text-primary" : "text-muted-foreground",
                    )}
                    aria-hidden
                  />
                  <button
                    type="button"
                    className="grid min-w-0 gap-0.5 text-left"
                    onClick={() => onSelectionChange(node.filePaths, !checked)}
                  >
                    <code
                      className="truncate font-mono text-xs text-foreground"
                      title={node.path}
                    >
                      {node.name}
                      {directory ? "/" : ""}
                    </code>
                    <small className="text-[11px] text-muted-foreground">
                      {detail}
                      {size === undefined
                        ? " · size unavailable"
                        : ` · ${size}`}
                    </small>
                  </button>
                </div>
              ) : (
                <div className="grid min-w-0 grid-cols-[16px_minmax(0,1fr)_auto] items-center gap-2">
                  <Icon
                    className={cn(
                      "size-4",
                      directory ? "text-primary" : "text-muted-foreground",
                    )}
                    aria-hidden
                  />
                  <code
                    className="truncate font-mono text-xs text-foreground"
                    title={node.path}
                  >
                    {node.name}
                    {directory ? "/" : ""}
                  </code>
                  {(directory || size !== undefined) && (
                    <small className="whitespace-nowrap text-[11px] text-muted-foreground">
                      {directory ? detail : ""}
                      {size === undefined
                        ? ""
                        : `${directory ? " · " : ""}${size}`}
                    </small>
                  )}
                </div>
              )}
            </div>
            {open && node.children.length > 0 && (
              <motion.div
                initial={reduce ? false : { opacity: 0, y: -4 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.22 }}
                className="overflow-hidden border-t border-border"
              >
                <Rows
                  nodes={node.children}
                  expanded={expanded}
                  searching={searching}
                  selected={selected}
                  onSelectionChange={onSelectionChange}
                  onExpand={onExpand}
                  depth={depth + 1}
                />
              </motion.div>
            )}
          </div>
        );
      })}
    </>
  );
}

export function FileListFrame({
  className,
  children,
}: {
  className?: string;
  children: ReactNode;
}) {
  return (
    <div
      className={cn(
        "max-h-[310px] overflow-y-auto overscroll-contain rounded-2xl border border-border bg-background",
        className,
      )}
    >
      {children}
    </div>
  );
}

function EmptyFilter() {
  return (
    <p className="px-4 py-5 text-center text-sm text-muted-foreground">
      No files match this filter.
    </p>
  );
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
  const visible = useMemo(
    () => filterTree(tree, normalizedQuery),
    [tree, normalizedQuery],
  );
  return (
    <>
      <Rows
        nodes={visible}
        expanded={expanded}
        searching={normalizedQuery.length > 0}
        selected={selected}
        onSelectionChange={onSelectionChange}
        onExpand={onExpand}
      />
      {visible.length === 0 && <EmptyFilter />}
    </>
  );
}

export function FileTree({
  files,
  title = "Files",
}: {
  files: FileTreeFile[];
  title?: string;
}) {
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const tree = useMemo(() => buildTree(files), [files]);
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const visible = useMemo(
    () => filterTree(tree, normalizedQuery),
    [tree, normalizedQuery],
  );
  function parts(nodes: FileTreeNode[]): ReactNode {
    return nodes.map((node) => {
      const size =
        node.totalSize === undefined
          ? "size unavailable"
          : formatBytes(node.totalSize);
      return node.kind === "directory" ? (
        <BeuiFolder
          key={node.path}
          value={node.path}
          name={`${node.name}/ · ${node.filePaths.length.toLocaleString()} ${node.filePaths.length === 1 ? "file" : "files"} · ${size}`}
        >
          {parts(node.children)}
        </BeuiFolder>
      ) : (
        <BeuiFile
          key={node.path}
          value={node.path}
          name={`${node.name} · ${size}`}
        />
      );
    });
  }
  function folders(nodes: FileTreeNode[]): string[] {
    return nodes.flatMap((node) =>
      node.kind === "directory" ? [node.path, ...folders(node.children)] : [],
    );
  }
  return (
    <section className="grid min-w-0 gap-2.5 pt-1">
      <div className="grid items-end gap-3.5 sm:grid-cols-[auto_minmax(220px,340px)] sm:justify-between">
        <div className="grid gap-0.5">
          <span className="text-xs text-muted-foreground">{title}</span>
          <strong className="text-[15px] font-semibold">
            {files.length.toLocaleString()}{" "}
            {files.length === 1 ? "file" : "files"}
          </strong>
        </div>
        <Input
          type="search"
          value={query}
          placeholder="Filter file tree"
          aria-label={`Filter ${title.toLocaleLowerCase()}`}
          leftIcon={<Search />}
          onChange={setQuery}
        />
      </div>
      <FileListFrame className="max-h-[390px] p-2">
        <BeuiFileTree
          ariaLabel={title}
          expandedIds={normalizedQuery ? folders(visible) : [...expanded]}
          onExpandedChange={(ids) => {
            if (!normalizedQuery) setExpanded(new Set(ids));
          }}
          classNames={{ label: "font-mono text-xs" }}
        >
          {parts(visible)}
        </BeuiFileTree>
        {visible.length === 0 && <EmptyFilter />}
      </FileListFrame>
    </section>
  );
}
