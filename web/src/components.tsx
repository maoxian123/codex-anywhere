import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  AlertCircle,
  Check,
  ChevronRight,
  Copy,
  ExternalLink,
  FileCode2,
  ImageIcon,
  LoaderCircle,
  X,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import type { Asset, Reference } from "./types";
import { imageSource, safeLink } from "./utils";

export function Spinner({ label = "加载中" }: { label?: string }) {
  return (
    <span className="spinner-wrap">
      <LoaderCircle className="spin" size={17} aria-hidden="true" />
      <span>{label}</span>
    </span>
  );
}
export function ErrorMessage({ children }: { children: ReactNode }) {
  return (
    <div className="inline-error" role="alert">
      <AlertCircle size={16} />
      <span>{children}</span>
    </div>
  );
}
export function Modal({
  title,
  subtitle,
  children,
  onClose,
  wide = false,
}: {
  title: string;
  subtitle?: string;
  children: ReactNode;
  onClose: () => void;
  wide?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    // showModal() can override React autoFocus by focusing the header's close
    // button. Prefer the dialog's editable controls after its native focus step.
    const active = document.activeElement;
    const enabled = (element: Element): element is HTMLElement =>
      element instanceof HTMLElement &&
      !element.matches(":disabled, [readonly], [type='hidden']") &&
      !element.closest("[hidden], [inert]");
    const autofocus = Array.from(dialog.querySelectorAll("[autofocus]")).find(
      enabled,
    );
    const editable = Array.from(
      dialog.querySelectorAll("input, textarea, select"),
    ).find(enabled);
    const button = Array.from(dialog.querySelectorAll("button")).find(enabled);
    const target =
      autofocus ||
      (active && dialog.contains(active) && enabled(active) ? active : null) ||
      editable ||
      button;
    dialog?.showModal();
    target?.focus();
    return () => dialog?.close();
  }, []);
  return (
    <dialog
      ref={ref}
      className={`modal ${wide ? "modal-wide" : ""}`}
      onCancel={onClose}
      onClick={(event) => {
        if (event.target === event.currentTarget) {
          const box = event.currentTarget.getBoundingClientRect();
          if (
            event.clientX < box.left ||
            event.clientX > box.right ||
            event.clientY < box.top ||
            event.clientY > box.bottom
          )
            onClose();
        }
      }}
    >
      <header className="modal-header">
        <div>
          <h2>{title}</h2>
          {subtitle && <p>{subtitle}</p>}
        </div>
        <button className="icon-button" onClick={onClose} aria-label="关闭窗口">
          <X size={19} />
        </button>
      </header>
      {children}
    </dialog>
  );
}
export function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );
  return (
    <button
      className="copy-button"
      title={error ? "无法访问剪贴板，请手动复制" : copied ? "已复制" : "复制"}
      aria-label={error ? "复制失败，请手动复制" : copied ? "已复制" : "复制"}
      onClick={() => {
        void navigator.clipboard
          ?.writeText(text)
          .then(() => {
            setCopied(true);
            setError(false);
            timer.current = setTimeout(() => setCopied(false), 2000);
          })
          .catch(() => setError(true));
        if (!navigator.clipboard) setError(true);
      }}
    >
      {copied ? <Check size={14} /> : <Copy size={14} />}
    </button>
  );
}
export function ImagePreview({
  src,
  alt = "图片",
  onOpen,
}: {
  src: string;
  alt?: string;
  onOpen: (src: string, alt: string) => void;
}) {
  const [failed, setFailed] = useState(false);
  if (failed)
    return (
      <span className="image-unavailable">
        <ImageIcon size={18} />
        图片暂不可用 · {alt}
      </span>
    );
  return (
    <button
      className="image-preview"
      onClick={() => onOpen(src, alt)}
      title={`查看图片：${alt}`}
    >
      <img loading="lazy" src={src} alt={alt} onError={() => setFailed(true)} />
    </button>
  );
}
export function Markdown({
  text,
  assets = [],
  onImage,
  onNavigate,
}: {
  text: string;
  assets?: Asset[];
  onImage: (src: string, alt: string) => void;
  onNavigate: (id: string, item?: string) => void;
}) {
  return (
    <div className="markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        urlTransform={(url, _key, node) =>
          node.tagName === "img"
            ? imageSource(url, assets) || ""
            : safeLink(url) || ""
        }
        components={{
          a: ({ href, children }) => {
            const safe = safeLink(href);
            if (!safe)
              return <span className="unavailable-link">{children}</span>;
            if (safe.startsWith("/tasks/"))
              return (
                <a
                  href={safe}
                  onClick={(event) => {
                    event.preventDefault();
                    const [path, query] = safe.split("?");
                    onNavigate(
                      path.split("/")[2],
                      new URLSearchParams(query).get("item") || undefined,
                    );
                  }}
                >
                  {children}
                </a>
              );
            return (
              <a href={safe} target="_blank" rel="noopener noreferrer">
                {children}
              </a>
            );
          },
          img: ({ src, alt }) =>
            src ? (
              <ImagePreview src={src} alt={alt} onOpen={onImage} />
            ) : (
              <span className="image-unavailable">
                <ImageIcon size={15} />
                {alt || "图片"} · 等待节点上传
              </span>
            ),
          pre: ({ children }) => <pre tabIndex={0}>{children}</pre>,
          table: ({ children }) => (
            <div className="table-scroll">
              <table>{children}</table>
            </div>
          ),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
export function References({
  references,
  onNavigate,
  removable,
}: {
  references: Reference[];
  onNavigate: (id: string, item?: string) => void;
  removable?: (index: number) => void;
}) {
  if (!references.length) return null;
  return (
    <div className="references">
      {references.map((reference, index) => {
        const title =
          reference.title ||
          (reference.kind === "task"
            ? "引用的任务"
            : reference.url || "网页引用");
        const content = (
          <>
            <span className="reference-icon">
              {reference.kind === "task" ? (
                <FileCode2 size={14} />
              ) : (
                <ExternalLink size={14} />
              )}
            </span>
            <span className="reference-title">{title}</span>
            {reference.item_id && (
              <span className="reference-detail">消息</span>
            )}
          </>
        );
        return (
          <div
            className={`reference-chip ${removable ? "removable" : ""}`}
            key={`${reference.snapshot_id || reference.thread_id || reference.url}-${index}`}
            title={reference.excerpt || title}
          >
            {reference.kind === "task" && reference.thread_id ? (
              <a
                href={`/tasks/${reference.thread_id}${reference.item_id ? `?item=${encodeURIComponent(reference.item_id)}` : ""}`}
                onClick={(event) => {
                  event.preventDefault();
                  onNavigate(reference.thread_id!, reference.item_id);
                }}
              >
                {content}
              </a>
            ) : safeLink(reference.url) ? (
              <a
                href={safeLink(reference.url)}
                target="_blank"
                rel="noopener noreferrer"
              >
                {content}
              </a>
            ) : (
              <span>{content}</span>
            )}
            {removable && (
              <button
                className="chip-remove"
                aria-label={`移除引用 ${title}`}
                onClick={() => removable(index)}
              >
                <X size={12} />
              </button>
            )}
          </div>
        );
      })}
    </div>
  );
}
export function AssetGallery({
  assets = [],
  onImage,
}: {
  assets?: Asset[];
  onImage: (src: string, alt: string) => void;
}) {
  const pictures = assets.filter(
    (asset) => asset.mime.startsWith("image/") && imageSource(asset.url),
  );
  if (!pictures.length) return null;
  return (
    <div className="asset-gallery">
      {pictures.map((asset) => (
        <figure key={asset.id}>
          <ImagePreview
            src={imageSource(asset.url)!}
            alt={asset.name}
            onOpen={onImage}
          />
          <figcaption>{asset.name}</figcaption>
        </figure>
      ))}
    </div>
  );
}
export function Disclosure({
  icon,
  title,
  meta,
  children,
  running = false,
  error = false,
}: {
  icon: ReactNode;
  title: string;
  meta?: string;
  children: ReactNode;
  running?: boolean;
  error?: boolean;
}) {
  return (
    <details className={`tool-card ${error ? "tool-error" : ""}`}>
      <summary>
        <ChevronRight className="disclosure-arrow" size={14} />
        <span className="tool-icon">
          {running ? <LoaderCircle className="spin" size={15} /> : icon}
        </span>
        <span className="tool-title">{title}</span>
        {meta && <span className="tool-meta">{meta}</span>}
      </summary>
      <div className="tool-content">{children}</div>
    </details>
  );
}
