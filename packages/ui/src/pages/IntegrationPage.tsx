import DOMPurify from "dompurify";
import { marked } from "marked";
import { ArrowUpRight } from "lucide-react";
import {
  memo,
  type RefObject,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { CodeBlock } from "@/components/agents/code-block";
import type { AgentCodeLanguage } from "@/components/agents/agent-code";
import { ButtonLink } from "@/components/motion/button/base";
import {
  ErrorBox,
  Loading,
  PageHeader,
  PageShell,
  Panel,
  useToast,
  writeClipboard,
} from "@/components/ui";

const languageAliases: Record<string, AgentCodeLanguage> = {
  bash: "bash",
  sh: "bash",
  shell: "bash",
  json: "json",
  diff: "diff",
  ts: "typescript",
  typescript: "typescript",
  tsx: "tsx",
  yaml: "yaml",
  yml: "yaml",
  ini: "ini",
  fstab: "ini",
};

function headingId(value: string) {
  return value
    .normalize("NFKD")
    .toLowerCase()
    .replace(/[^\p{Letter}\p{Number}]+/gu, "-")
    .replace(/^-|-$/g, "");
}

function renderMarkdown(markdown: string) {
  const headings: Array<{ id: string; title: string }> = [];
  const usedIds = new Map<string, number>();
  const blocks: Array<{
    code: string;
    language: AgentCodeLanguage;
  }> = [];
  const renderer = new marked.Renderer();
  renderer.heading = function ({ tokens, depth, text }) {
    const content = this.parser.parseInline(tokens);
    const base = headingId(text);
    const count = usedIds.get(base) ?? 0;
    usedIds.set(base, count + 1);
    const id = count ? `${base}-${count}` : base;
    if (depth === 2) headings.push({ id, title: text.replace(/[*_`]/g, "") });
    return `<h${depth} id="${id}">${content}</h${depth}>`;
  };
  renderer.code = ({ text, lang }) => {
    const requested =
      lang?.trim().split(/\s+/, 1)[0]?.toLowerCase() ?? "plaintext";
    const index =
      blocks.push({
        code: text,
        language: languageAliases[requested] ?? "text",
      }) - 1;
    return `<div data-code-block="${index}"></div>`;
  };
  const rendered = marked.parse(markdown, { renderer });
  if (typeof rendered !== "string")
    throw new Error("Markdown renderer returned no document");
  return { html: DOMPurify.sanitize(rendered), blocks, headings };
}

export function IntegrationPage() {
  const [markdown, setMarkdown] = useState("");
  const [error, setError] = useState("");
  const rendered = useMemo(
    () => (markdown ? renderMarkdown(markdown) : null),
    [markdown],
  );
  const articleRef = useRef<HTMLElement>(null);
  const [codeTargets, setCodeTargets] = useState<Element[]>([]);
  const { notify } = useToast();

  useEffect(() => {
    setCodeTargets(
      Array.from(
        articleRef.current?.querySelectorAll("[data-code-block]") ?? [],
      ),
    );
    if (window.location.hash) {
      try {
        document
          .getElementById(decodeURIComponent(window.location.hash.slice(1)))
          ?.scrollIntoView();
      } catch {
        /* A malformed fragment does not prevent reading the guide. */
      }
    }
  }, [rendered]);

  useEffect(() => {
    const controller = new AbortController();
    void fetch("/integration.md", {
      credentials: "same-origin",
      cache: "no-store",
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) {
          const detail = (await response.text()).trim();
          throw new Error(
            `HTTP ${response.status}${detail ? `: ${detail}` : ""}`,
          );
        }
        return response.text();
      })
      .then(setMarkdown)
      .catch((cause: unknown) => {
        if (cause instanceof DOMException && cause.name === "AbortError")
          return;
        setError(cause instanceof Error ? cause.message : String(cause));
      });
    return () => controller.abort();
  }, []);

  return (
    <PageShell className="scroll-smooth">
      <PageHeader
        eyebrow="Consume the shelf"
        title="Integration"
        actions={
          <ButtonLink
            variant="outline"
            href="/integration.md"
            target="_blank"
            rel="noreferrer"
          >
            For agents · Markdown
            <ArrowUpRight className="size-4" aria-hidden />
          </ButtonLink>
        }
      />
      {error && (
        <ErrorBox>Could not load integration documentation: {error}</ErrorBox>
      )}
      {!error && !markdown && (
        <Panel className="p-6">
          <Loading className="justify-start">
            Loading integration documentation…
          </Loading>
        </Panel>
      )}
      {rendered && (
        <div className="grid min-w-0 items-start gap-8 lg:grid-cols-[220px_minmax(0,1fr)] lg:gap-12">
          {rendered.headings.length > 0 && (
            <aside
              className="rounded-2xl border border-border bg-card p-4 lg:sticky lg:top-24"
              aria-label="On this page"
            >
              <p className="mb-3 px-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                On this page
              </p>
              <ul className="grid gap-1">
                {rendered.headings.map((heading) => (
                  <li key={heading.id}>
                    <a
                      className="flex min-h-11 items-center rounded-lg px-2 py-2 text-sm leading-relaxed text-muted-foreground transition-colors hover:bg-muted hover:text-primary"
                      href={`#${heading.id}`}
                      onClick={() => {
                        requestAnimationFrame(() => {
                          const target = document.getElementById(heading.id);
                          target?.setAttribute("tabindex", "-1");
                          target?.focus({ preventScroll: true });
                        });
                      }}
                    >
                      {heading.title}
                    </a>
                  </li>
                ))}
              </ul>
            </aside>
          )}
          <div
            className={
              rendered.headings.length ? "min-w-0" : "min-w-0 lg:col-span-2"
            }
          >
            <MarkdownArticle html={rendered.html} articleRef={articleRef} />
          </div>
        </div>
      )}
      {rendered &&
        codeTargets.map((target, index) => {
          const block = rendered.blocks[index];
          return (
            block &&
            createPortal(
              <CodeBlock
                code={block.code}
                language={block.language}
                showLineNumbers={false}
                maxHeight={480}
                onCopy={() => writeClipboard(block.code)}
                onCopyError={(cause) =>
                  notify({
                    status: "error",
                    title: "Could not copy code",
                    description:
                      cause instanceof Error ? cause.message : String(cause),
                  })
                }
              />,
              target,
              String(index),
            )
          );
        })}
    </PageShell>
  );
}

// The sanitized HTML owns these nodes; memo keeps portal targets stable when
// code blocks or toast state update the surrounding React tree.
const MarkdownArticle = memo(function MarkdownArticle({
  html,
  articleRef,
}: {
  html: string;
  articleRef: RefObject<HTMLElement | null>;
}) {
  return (
    <article
      className="integration-markdown"
      ref={articleRef}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
});
