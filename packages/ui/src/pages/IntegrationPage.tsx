import DOMPurify from "dompurify";
import hljs from "highlight.js/lib/core";
import bash from "highlight.js/lib/languages/bash";
import ini from "highlight.js/lib/languages/ini";
import plaintext from "highlight.js/lib/languages/plaintext";
import yaml from "highlight.js/lib/languages/yaml";
import { marked } from "marked";
import { type MouseEvent, useEffect, useMemo, useState } from "react";

hljs.registerLanguage("bash", bash);
hljs.registerLanguage("yaml", yaml);
hljs.registerLanguage("ini", ini);
hljs.registerLanguage("plaintext", plaintext);

const languageAliases: Record<string, string> = {
  bash: "bash",
  sh: "bash",
  shell: "bash",
  yaml: "yaml",
  yml: "yaml",
  ini: "ini",
  fstab: "ini",
  text: "plaintext",
  plaintext: "plaintext",
};

function headingId(value: string) {
  return value
    .normalize("NFKD")
    .toLowerCase()
    .replace(/[^\p{Letter}\p{Number}]+/gu, "-")
    .replace(/^-|-$/g, "");
}

function renderMarkdown(markdown: string) {
  const renderer = new marked.Renderer();
  renderer.heading = function ({ tokens, depth, text }) {
    const content = this.parser.parseInline(tokens);
    return `<h${depth} id="${headingId(text)}">${content}</h${depth}>`;
  };
  renderer.code = ({ text, lang }) => {
    const requested = lang?.trim().split(/\s+/, 1)[0]?.toLowerCase() ?? "plaintext";
    const language = languageAliases[requested] ?? "plaintext";
    const highlighted = hljs.highlight(text, { language, ignoreIllegals: true }).value;
    return `<div class="copy-block highlighted-code-block"><pre><code class="hljs language-${language}">${highlighted}</code></pre><button class="small" type="button" data-copy-code>Copy</button></div>`;
  };
  const rendered = marked.parse(markdown, { renderer });
  if (typeof rendered !== "string") throw new Error("Markdown renderer returned no document");
  return DOMPurify.sanitize(rendered);
}

async function copyText(value: string) {
  if (navigator.clipboard) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const input = document.createElement("textarea");
  input.value = value;
  input.style.position = "fixed";
  input.style.opacity = "0";
  document.body.append(input);
  input.select();
  const succeeded = document.execCommand("copy");
  input.remove();
  if (!succeeded) throw new Error("clipboard API unavailable");
}

export function IntegrationPage() {
  const [markdown, setMarkdown] = useState("");
  const [error, setError] = useState("");
  const rendered = useMemo(() => (markdown ? renderMarkdown(markdown) : ""), [markdown]);

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
          throw new Error(`HTTP ${response.status}${detail ? `: ${detail}` : ""}`);
        }
        return response.text();
      })
      .then(setMarkdown)
      .catch((cause: unknown) => {
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        setError(cause instanceof Error ? cause.message : String(cause));
      });
    return () => controller.abort();
  }, []);

  async function handleDocumentClick(event: MouseEvent<HTMLElement>) {
    const target = event.target;
    if (!(target instanceof HTMLButtonElement) || !target.hasAttribute("data-copy-code")) return;
    const code = target.parentElement?.querySelector("code")?.textContent;
    if (code === undefined) return;
    try {
      await copyText(code);
      target.textContent = "Copied";
    } catch {
      target.textContent = "Copy failed";
    }
    window.setTimeout(() => {
      target.textContent = "Copy";
    }, 1800);
  }

  return (
    <div className="page integration-page">
      <div className="page-head">
        <div>
          <p className="eyebrow">Consume the shelf</p>
          <h1>Integration</h1>
        </div>
        <a
          className="button secondary integration-agent-link"
          href="/integration.md"
          target="_blank"
          rel="noreferrer"
        >
          For agents · Markdown ↗
        </a>
      </div>

      {error && (
        <div className="error-box">Could not load integration documentation: {error}</div>
      )}
      {!error && !markdown && (
        <div className="panel integration-loading">Loading integration documentation…</div>
      )}
      {rendered && (
        <article
          className="integration-markdown"
          onClick={(event) => void handleDocumentClick(event)}
          dangerouslySetInnerHTML={{ __html: rendered }}
        />
      )}
    </div>
  );
}
