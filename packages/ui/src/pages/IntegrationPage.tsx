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
  const blocks: Array<{
    code: string;
    language: AgentCodeLanguage;
  }> = [];
  const renderer = new marked.Renderer();
  renderer.heading = function ({ tokens, depth, text }) {
    const content = this.parser.parseInline(tokens);
    return `<h${depth} id="${headingId(text)}">${content}</h${depth}>`;
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
  return { html: DOMPurify.sanitize(rendered), blocks };
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
        <MarkdownArticle html={rendered.html} articleRef={articleRef} />
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
