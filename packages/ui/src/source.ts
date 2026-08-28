import type { Provider } from "./types.ts";

export function sourceModelUrl(
  provider: Provider,
  sourceId: string,
  revision?: string,
): string | undefined {
  const sourcePath = sourceId.split("/").map(encodeURIComponent).join("/");
  if (provider === "huggingface") {
    return revision
      ? `https://huggingface.co/${sourcePath}/tree/${encodeURIComponent(revision)}`
      : `https://huggingface.co/${sourcePath}`;
  }
  if (provider === "modelscope-cn") return `https://modelscope.cn/models/${sourcePath}`;
  if (provider === "modelscope-ai") return `https://modelscope.ai/models/${sourcePath}`;
  if (provider === "github-release") {
    const tag = revision?.startsWith("release:")
      ? revision.split(":").slice(2).join(":")
      : revision;
    return tag
      ? `https://github.com/${sourcePath}/releases/tag/${encodeURIComponent(tag)}`
      : `https://github.com/${sourcePath}/releases`;
  }
  if (provider === "kaggle") return `https://www.kaggle.com/models/${sourcePath}`;
  if (provider === "http" && /^https?:\/\//i.test(sourceId)) return sourceId;
  return undefined;
}
