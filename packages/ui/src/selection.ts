export function selectionSummary(paths?: string[]): string {
  if (!paths) return "Full repository";
  const sorted = [...new Set(paths)].sort((left, right) => left.localeCompare(right));
  if (sorted.length === 0) return "No files selected";

  const ggufPaths = sorted.filter((path) => path.toLocaleLowerCase().endsWith(".gguf"));
  if (ggufPaths.length === sorted.length) {
    const parents = [...new Set(ggufPaths.map((path) => path.split("/").slice(0, -1).join("/")))];
    if (parents.length === 1 && parents[0]) {
      return `GGUF · ${parents[0]}`;
    }
    if (ggufPaths.length === 1) {
      return `GGUF · ${ggufPaths[0].split("/").at(-1)}`;
    }
    const first = ggufPaths[0].split("/").at(-1) ?? "variant";
    const variant = first
      .replace(/-\d{5}-of-\d{5}\.gguf$/i, "")
      .replace(/\.gguf$/i, "");
    return `GGUF · ${variant}`;
  }

  const rootFiles = sorted.filter((path) => !path.includes("/"));
  const roots = [...new Set(sorted.filter((path) => path.includes("/")).map((path) => path.split("/", 1)[0]))];
  const rootPart = rootFiles.length > 0
    ? `${rootFiles.length} root ${rootFiles.length === 1 ? "file" : "files"}`
    : "";
  if (roots.length === 0) return rootPart;
  const folderPart = roots.length <= 2
    ? roots.map((root) => `${root}/`).join(" + ")
    : `${roots.length} top-level folders`;
  return rootPart ? `${folderPart} + ${rootPart}` : folderPart;
}
